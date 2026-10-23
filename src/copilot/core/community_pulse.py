"""Community Pulse: deterministic, evidence-backed community intelligence.

The service reads only guild-scoped summaries and approved/unapproved KB rows.
It never profiles members, ranks people, invents engagement numbers, or claims a
causal effect. Observations are facts read from stored rows; recommendations are
explicitly estimates with evidence and an uncertainty label.

Recommendation ids are a hash of the observed key, so rerunning Pulse over the
same data returns the same id. A changed data set can safely make an old id
disappear; the draft bridge never trusts an id supplied by model output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..errors import PulseRecommendationNotFoundError
from ..util.text import neutralize_mentions, truncate
from .content_studio import (
    ContentDraftView,
    ContentStudio,
    ContentTarget,
    ContentType,
    DraftRequest,
)
from .insights_engine import compute_insights

log = logging.getLogger(__name__)

_OPEN_STATUSES = {"unresolved", "in_progress", "waiting_for_user", "unclear"}
_MIN_REPEAT = 2
_MAX_CLUSTERS = 5
_MAX_RECOMMENDATIONS = 8
_MAX_EVIDENCE = 5
_MAX_OBSERVATIONS = 20
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")
_TOPIC_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "because",
        "been",
        "before",
        "being",
        "could",
        "does",
        "doesn",
        "doing",
        "error",
        "from",
        "have",
        "help",
        "into",
        "issue",
        "just",
        "problem",
        "should",
        "still",
        "that",
        "their",
        "there",
        "these",
        "they",
        "this",
        "thread",
        "with",
        "what",
        "when",
        "where",
        "which",
        "while",
        "would",
        "your",
    }
)


class PulseEvidence(BaseModel):
    """A bounded, reproducible evidence pointer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=32)
    ref_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=1000)
    snippet: str = Field(default="", max_length=400)


class PulseObservation(BaseModel):
    """A fact observed in stored data; never a recommendation by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=48)
    title: str = Field(min_length=1, max_length=300)
    count: int = Field(ge=0)
    evidence: list[PulseEvidence] = Field(default_factory=list, max_length=_MAX_EVIDENCE)
    note: str = Field(default="", max_length=500)


class PulseRecommendation(BaseModel):
    """An explicitly uncertain action proposal backed by evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: str = Field(min_length=8, max_length=32)
    kind: Literal[
        "faq", "tutorial", "documentation", "event", "onboarding", "weekly_content"
    ]
    title: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=1000)
    evidence: list[PulseEvidence] = Field(min_length=1, max_length=_MAX_EVIDENCE)
    suggested_action: str = Field(min_length=1, max_length=800)
    expected_outcome: str = Field(min_length=1, max_length=500)
    uncertainty: Literal["low", "medium", "high"]


class CommunityPulse(BaseModel):
    """One deterministic Pulse snapshot for one guild and window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_days: int = Field(ge=1)
    minimum_threads: int = Field(ge=1)
    total_threads: int = Field(ge=0)
    sufficient: bool
    status_counts: dict[str, int] = Field(default_factory=dict)
    threads_with_open_questions: int = Field(ge=0)
    observations: list[PulseObservation] = Field(default_factory=list, max_length=_MAX_OBSERVATIONS)
    recommendations: list[PulseRecommendation] = Field(
        default_factory=list, max_length=_MAX_RECOMMENDATIONS
    )


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _load_object(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_signatures(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _label(text: str, limit: int = 160) -> str:
    return _SPACE_RE.sub(" ", (text or "").strip())[:limit]


def _key(text: str) -> str:
    return _PUNCT_RE.sub(" ", (text or "").lower()).strip()


def _recommendation_id(kind: str, key: str) -> str:
    digest = hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:12]
    return f"pulse-{digest}"


def _uncertainty(count: int) -> Literal["low", "medium", "high"]:
    if count >= 5:
        return "low"
    if count >= 3:
        return "medium"
    return "high"


def _thread_url(row: Any) -> str | None:
    guild_id = row["guild_id"]
    thread_id = row["discord_thread_id"]
    if guild_id and thread_id:
        return f"https://discord.com/channels/{guild_id}/{thread_id}"
    return None


def _evidence(row: Any) -> PulseEvidence:
    return PulseEvidence(
        kind="thread",
        ref_id=str(row["discord_thread_id"]),
        title=_label(str(row["thread_name"] or "Thread")) or "Thread",
        url=_thread_url(row),
    )


def _unique_evidence(items: Sequence[PulseEvidence]) -> list[PulseEvidence]:
    seen: set[tuple[str, str]] = set()
    out: list[PulseEvidence] = []
    for item in items:
        key = (item.kind, item.ref_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= _MAX_EVIDENCE:
            break
    return out


def _latest_per_thread(rows: Sequence[Any]) -> list[Any]:
    seen: set[int] = set()
    latest: list[Any] = []
    for row in rows:
        thread_id = int(row["thread_id"])
        if thread_id in seen:
            continue
        seen.add(thread_id)
        latest.append(row)
    return latest


def _cluster(rows: Sequence[Any], values: Callable[[Any], list[str]]):
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        for value in values(row):
            label = _label(value)
            key = _key(label)
            if len(key) < 8:
                continue
            group = groups.setdefault(key, {"label": label, "rows": {}, "count": 0})
            group["rows"][int(row["thread_id"])] = row
            group["count"] = len(group["rows"])
    clustered = [
        (item["count"], item["label"], list(item["rows"].values()))
        for item in groups.values()
        if item["count"] >= _MIN_REPEAT
    ]
    clustered.sort(key=lambda item: (-item[0], item[1].lower()))
    return clustered[:_MAX_CLUSTERS]


def _topic_observations(rows: Sequence[Any], *, limit: int = 3) -> list[PulseObservation]:
    """Recurring words in stored thread names, one observation per topic.

    This is a lexical frequency count over thread names, not topic modelling and
    not a claim about what members think. A topic needs at least two distinct
    threads, and only the most frequent bounded topics are returned.
    """
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = _label(str(row["thread_name"] or ""), 300)
        seen: set[str] = set()
        for token in _PUNCT_RE.sub(" ", name.lower()).split():
            if len(token) < 4 or token.isdigit() or token in _TOPIC_STOPWORDS or token in seen:
                continue
            seen.add(token)
            group = groups.setdefault(token, {"rows": {}})
            group["rows"][int(row["thread_id"])] = row

    ordered = sorted(
        (
            (len(group["rows"]), token, list(group["rows"].values()))
            for token, group in groups.items()
        ),
        key=lambda item: (-item[0], item[1]),
    )
    observations: list[PulseObservation] = []
    for count, token, cluster_rows in ordered[:limit]:
        if count < _MIN_REPEAT:
            continue
        observations.append(
            PulseObservation(
                kind="common_topic",
                title=f"Common thread-name topic: {token}",
                count=count,
                evidence=_unique_evidence([_evidence(row) for row in cluster_rows]),
                note=(
                    "Lexical word frequency across distinct thread names; not a member "
                    "profile and not a claim about interest or engagement."
                ),
            )
        )
    return observations


def _questions(row: Any) -> list[str]:
    analysis = _load_object(row["analysis_json"])
    questions = analysis.get("unanswered_questions")
    if not isinstance(questions, list):
        return []
    return [item for item in questions if isinstance(item, str)]


def _signatures(row: Any) -> list[str]:
    return _load_signatures(row["error_signatures"])


def _make_recommendation(
    *,
    kind: Literal[
        "faq", "tutorial", "documentation", "event", "onboarding", "weekly_content"
    ],
    key: str,
    title: str,
    rationale: str,
    evidence: Sequence[PulseEvidence],
    action: str,
    outcome: str,
    count: int,
) -> PulseRecommendation:
    return PulseRecommendation(
        recommendation_id=_recommendation_id(kind, key),
        kind=kind,
        title=_label(title, 300),
        rationale=_label(rationale, 1000),
        evidence=_unique_evidence(evidence),
        suggested_action=_label(action, 800),
        expected_outcome=_label(outcome, 500),
        uncertainty=_uncertainty(count),
    )


async def _approved_match(repo: Repository, text: str, *, guild_id: str) -> Any | None:
    rows = await repo.search_kb(text, guild_id=guild_id, approved_only=True, limit=1)
    return rows[0] if rows else None


async def compute_community_pulse(
    repo: Repository,
    *,
    guild_id: str,
    window_days: int,
    minimum_threads: int,
    clock: Callable[[], datetime] | None = None,
) -> CommunityPulse:
    """Compute one guild-scoped, minimum-data-gated Pulse snapshot."""
    insights = await compute_insights(
        repo,
        guild_id=str(guild_id),
        window_days=window_days,
        minimum_threads=minimum_threads,
        clock=clock,
    )
    if not insights.sufficient:
        return CommunityPulse(
            window_days=window_days,
            minimum_threads=minimum_threads,
            total_threads=insights.total_threads,
            sufficient=False,
            threads_with_open_questions=insights.threads_with_open_questions,
        )

    now = _as_utc((clock or (lambda: datetime.now(UTC)))())
    since = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    rows = await repo.pulse_summaries_since(since, guild_id=str(guild_id))
    latest = _latest_per_thread(rows)
    if not latest:
        return CommunityPulse(
            window_days=window_days,
            minimum_threads=minimum_threads,
            total_threads=insights.total_threads,
            sufficient=False,
            threads_with_open_questions=insights.threads_with_open_questions,
        )

    observations: list[PulseObservation] = []
    if insights.status_counts:
        observations.append(
            PulseObservation(
                kind="status_distribution",
                title="Issue status distribution",
                count=sum(insights.status_counts.values()),
                note="Counts of stored summary statuses; no member profile data.",
            )
        )

    open_rows = [row for row in latest if str(row["status"]) in _OPEN_STATUSES]
    if open_rows:
        observations.append(
            PulseObservation(
                kind="unresolved_threads",
                title="Unresolved or unclear threads",
                count=len(open_rows),
                evidence=_unique_evidence([_evidence(row) for row in open_rows[:_MAX_EVIDENCE]]),
                note="Stored summary status only; a human must verify the current thread.",
            )
        )

    question_clusters = _cluster(latest, _questions)
    error_clusters = _cluster(latest, _signatures)

    observations.extend(_topic_observations(latest))

    for count, label, cluster_rows in question_clusters:
        observations.append(
            PulseObservation(
                kind="repeated_question",
                title=label,
                count=count,
                evidence=_unique_evidence([_evidence(row) for row in cluster_rows]),
                note="Normalized-text grouping across distinct summarized threads.",
            )
        )
    for count, label, cluster_rows in error_clusters:
        observations.append(
            PulseObservation(
                kind="recurring_problem",
                title=label,
                count=count,
                evidence=_unique_evidence([_evidence(row) for row in cluster_rows]),
                note="Repeated error signatures across distinct summarized threads.",
            )
        )

    # Reuse the existing FTS5/BM25 duplicate path for possible related discussions.
    for _count, label, _cluster_rows in (question_clusters + error_clusters)[:2]:
        related = await repo.related_docs(label, guild_id=str(guild_id), limit=3)
        if not related:
            continue
        evidence = [
            PulseEvidence(
                kind="kb_doc",
                ref_id=str(row["id"]),
                title=_label(str(row["title"] or "Related discussion")) or "Related discussion",
                url=row["url"],
                snippet=_label(str(row["snippet"] or ""), 400),
            )
            for row in related
        ]
        observations.append(
            PulseObservation(
                kind="similar_discussion",
                title=f"Possibly related to: {label}",
                count=len(evidence),
                evidence=_unique_evidence(evidence),
                note="Lexical BM25 match only; nothing is merged automatically.",
            )
        )

    recommendations: list[tuple[int, PulseRecommendation]] = []
    coverage_notes: list[PulseObservation] = []

    for count, label, cluster_rows in question_clusters:
        evidence = [_evidence(row) for row in cluster_rows]
        match = await _approved_match(repo, label, guild_id=str(guild_id))
        if match is None:
            coverage_notes.append(
                PulseObservation(
                    kind="knowledge_gap",
                    title=label,
                    count=count,
                    evidence=_unique_evidence(evidence),
                    note="No approved knowledge-base document matched this topic.",
                )
            )
            recommendations.append(
                (
                    count,
                    _make_recommendation(
                        kind="faq",
                        key=f"faq:{_key(label)}",
                        title=f"Answer the recurring question: {label}",
                        rationale=(
                            f"Observed {count} summarized thread(s) in the last {window_days} days "
                            "with this unanswered question; an approved KB search returned no match."
                        ),
                        evidence=evidence,
                        action=(
                            "Draft a concise FAQ answer with links to the source threads, then have "
                            "a moderator review it through the knowledge-base workflow."
                        ),
                        outcome=(
                            "Estimate (not measured): readers may find an answer faster and repeat "
                            "this question less often."
                        ),
                        count=count,
                    ),
                )
            )
        if count >= 3:
            recommendations.append(
                (
                    count,
                    _make_recommendation(
                        kind="event",
                        key=f"event:{_key(label)}",
                        title=f"Host a community Q&A on: {label}",
                        rationale=(
                            f"Observed {count} summarized thread(s) in the last {window_days} days "
                            "asking the same or a normalized-equivalent question."
                        ),
                        evidence=evidence,
                        action=(
                            "Ask a moderator to plan an optional office-hours or Q&A session; no "
                            "event is created, scheduled, or announced automatically."
                        ),
                        outcome=(
                            "Estimate (not measured): a live Q&A may clarify recurring confusion."
                        ),
                        count=count,
                    ),
                )
            )

    for count, label, cluster_rows in error_clusters:
        evidence = [_evidence(row) for row in cluster_rows]
        if await _approved_match(repo, label, guild_id=str(guild_id)) is not None:
            continue
        coverage_notes.append(
            PulseObservation(
                kind="knowledge_gap",
                title=label,
                count=count,
                evidence=_unique_evidence(evidence),
                note="No approved knowledge-base document matched this recurring problem.",
            )
        )
        # Recurring technical problems become a documentation update; smaller
        # clusters are framed as a tutorial instead of over-claiming a docs gap.
        kind: Literal["tutorial", "documentation"] = (
            "documentation" if count >= 4 else "tutorial"
        )
        recommendations.append(
            (
                count,
                _make_recommendation(
                    kind=kind,
                    key=f"{kind}:{_key(label)}",
                    title=(
                        f"Update documentation for: {label}"
                        if kind == "documentation"
                        else f"Create a walkthrough for: {label}"
                    ),
                    rationale=(
                        f"Observed {count} summarized thread(s) in the last {window_days} days with "
                        "the same normalized error signature; no approved KB document matched it."
                    ),
                    evidence=evidence,
                    action=(
                        "Draft a documentation update or short troubleshooting walkthrough and "
                        "submit it for human review."
                    ),
                    outcome=(
                        "Estimate (not measured): clearer troubleshooting documentation may "
                        "shorten repeat help; this is not a measured improvement."
                    ),
                    count=count,
                ),
            )
        )

    top_clusters = question_clusters + error_clusters
    if top_clusters:
        count, label, cluster_rows = sorted(
            top_clusters, key=lambda item: (-item[0], item[1].lower())
        )[0]
        if count >= 3:
            recommendations.append(
                (
                    count,
                    _make_recommendation(
                        kind="weekly_content",
                        key=f"weekly:{_key(label)}",
                        title=f"Weekly content idea: {label}",
                        rationale=(
                            f"Observed {count} summarized thread(s) in the last {window_days} "
                            "days on this normalized topic. This is a content idea, not a "
                            "measured engagement opportunity."
                        ),
                        evidence=[_evidence(row) for row in cluster_rows],
                        action=(
                            "Consider one short weekly post, FAQ entry, or small coding challenge "
                            "on this topic; a moderator must draft and review it through Content "
                            "Studio before anything is published."
                        ),
                        outcome=(
                            "Estimate (not measured): recurring content may help members find "
                            "this answer; no engagement or improvement is claimed."
                        ),
                        count=count,
                    ),
                )
            )

    if open_rows and len(open_rows) >= _MIN_REPEAT:
        recommendations.append(
            (
                len(open_rows),
                _make_recommendation(
                    kind="onboarding",
                    key=f"onboarding:{len(open_rows)}:{window_days}",
                    title="Improve onboarding for unresolved threads",
                    rationale=(
                        f"{len(open_rows)} summarized thread(s) in the last {window_days} days are "
                        "stored as unresolved, in progress, waiting for user, or unclear."
                    ),
                    evidence=[_evidence(row) for row in open_rows],
                    action=(
                        "Review those threads, record any confirmed resolutions, and consider an "
                        "onboarding checklist for complete issue reports."
                    ),
                    outcome=(
                        "Estimate (not measured): better reports may reduce back-and-forth; no "
                        "causal effect is asserted."
                    ),
                    count=len(open_rows),
                ),
            )
        )

    observations.extend(coverage_notes[: max(0, _MAX_OBSERVATIONS - len(observations))])
    chosen = sorted(
        recommendations,
        key=lambda item: (-item[0], item[1].kind, item[1].title.lower()),
    )[:_MAX_RECOMMENDATIONS]

    return CommunityPulse(
        window_days=window_days,
        minimum_threads=minimum_threads,
        total_threads=insights.total_threads,
        sufficient=True,
        status_counts=insights.status_counts,
        threads_with_open_questions=insights.threads_with_open_questions,
        observations=observations[:_MAX_OBSERVATIONS],
        recommendations=[rec for _count, rec in chosen],
    )


def _draft_content(rec: PulseRecommendation, *, max_chars: int) -> str:
    evidence = list(rec.evidence)
    while True:
        lines = [
            neutralize_mentions(rec.title),
            "",
            "Observation and rationale:",
            neutralize_mentions(rec.rationale),
            "",
            "Suggested next step:",
            neutralize_mentions(rec.suggested_action),
            "",
            "Expected outcome:",
            neutralize_mentions(rec.expected_outcome),
            "",
            "Evidence:",
        ]
        for item in evidence:
            source = item.url or f"source ref {item.ref_id}"
            lines.append(f"- {neutralize_mentions(item.title)}: {source}")
        lines.extend(
            [
                "",
                f"Uncertainty: {rec.uncertainty}. This is an observational estimate, not a "
                "measured or causal claim.",
            ]
        )
        text = "\n".join(lines)
        if len(text) <= max_chars or not evidence:
            return truncate(text, max_chars)
        evidence.pop()


def _content_type(rec: PulseRecommendation) -> ContentType:
    if rec.kind == "event":
        return ContentType.event_announcement
    if rec.kind in {"faq", "tutorial", "documentation", "weekly_content"}:
        return ContentType.educational_post
    return ContentType.announcement


async def create_draft_from_recommendation(
    repo: Repository,
    studio: ContentStudio,
    actor: Any,
    *,
    recommendation_id: str,
    target: ContentTarget,
    settings: Any,
    window_days: int | None = None,
    minimum_threads: int | None = None,
) -> ContentDraftView:
    """Create one Content Studio draft from a reproducible Pulse recommendation.

    This is the entire write: it never submits, approves, schedules, or publishes.
    The recommendation id is recomputed from current guild data; a stale id is
    rejected instead of trusting text supplied by a caller or model.
    """
    pulse = await compute_community_pulse(
        repo,
        guild_id=str(actor.guild_id),
        window_days=window_days or settings.insights_window_days,
        minimum_threads=minimum_threads or settings.insights_min_threads,
    )
    rec = next(
        (item for item in pulse.recommendations if item.recommendation_id == recommendation_id),
        None,
    )
    if rec is None:
        raise PulseRecommendationNotFoundError()

    request = DraftRequest(
        content_type=_content_type(rec),
        topic=truncate(rec.title, 500),
        audience="Community members",
        tone="Helpful and factual",
        purpose="Convert a Community Pulse estimate into a draft; human review is required.",
    )
    content = _draft_content(rec, max_chars=settings.content_generation_max_chars)
    view = await studio.create_draft(actor, request, content, target=target)
    await repo.audit(
        actor_id=actor.actor_id,
        action="pulse.draft_created",
        target_type="content_draft",
        target_id=str(view.draft_id),
        meta={
            "guild_id": actor.guild_id,
            "recommendation_id": rec.recommendation_id,
            "recommendation_kind": rec.kind,
            "uncertainty": rec.uncertainty,
        },
    )
    return view
