"""Phase 1 read-only service adapters.

Only capabilities whose business logic already exists and is safe to call
without Discord are registered: approved knowledge search and the existing
guild-scoped insights engine. Thread summarization/debug remain command/queue
paths because they require a Discord thread or a separate dev-only boundary.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from ..core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from ..core.community_pulse import CommunityPulse, compute_community_pulse
from ..core.insights_engine import compute_insights
from ..core.knowledge_candidates import list_candidates
from ..db.repository import Repository
from ..errors import ResearchNoResultsError, ResearchProviderError, ResearchUnavailableError
from ..research.service import ResearchService, ResearchStatus
from ..util.text import sanitize_for_prompt, truncate
from .contract import (
    Reference,
    Skill,
    SkillContext,
    SkillErrorPolicy,
    SkillInput,
    SkillLimits,
    SkillOutput,
    SkillPermission,
)
from .registry import SkillRegistry


class KbSearchInput(SkillInput):
    query: str = Field(min_length=3, max_length=200)
    limit: int = Field(default=5, ge=1, le=5)


class KbSearchOutput(SkillOutput):
    query: str


class KbSearchSkill(Skill[KbSearchInput, KbSearchOutput]):
    """Search only approved, guild-scoped knowledge for a bounded result set."""

    skill_id = "kb.search"
    description = "Search moderator-approved knowledge in the caller's guild."
    input_model = KbSearchInput
    output_model = KbSearchOutput
    permissions = frozenset({SkillPermission.guild_member})
    limits = SkillLimits(timeout_s=2.0, max_output_chars=6_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def execute(self, context: SkillContext, payload: KbSearchInput) -> KbSearchOutput:
        rows = await self._repo.search_kb(
            payload.query,
            guild_id=str(context.guild_id),
            limit=payload.limit,
            approved_only=True,
        )
        references = [
            Reference(
                kind=truncate(sanitize_for_prompt(str(row["kind"])), 32) or "unknown",
                title=truncate(sanitize_for_prompt(str(row["title"] or "untitled")), 200) or "untitled",
                url=truncate(str(row["url"]), 1000) if row["url"] else None,
                snippet=truncate(sanitize_for_prompt(str(row["snippet"] or row["body"] or "")), 400),
            )
            for row in rows
        ]
        return KbSearchOutput(query=payload.query, references=references)


class InsightsInput(SkillInput):
    """No caller input; the guild comes from the authorized context."""


class InsightsOutput(SkillOutput):
    window_days: int
    minimum_threads: int
    total_threads: int
    sufficient: bool
    status_counts: dict[str, int]
    top_errors: list[tuple[str, int]]
    threads_with_open_questions: int


class InsightsSkill(Skill[InsightsInput, InsightsOutput]):
    """Expose the existing minimum-data-gated aggregate as a moderator skill."""

    skill_id = "insights.community"
    description = "Return guild-scoped aggregate community insights for moderators."
    input_model = InsightsInput
    output_model = InsightsOutput
    permissions = frozenset({SkillPermission.moderator})
    limits = SkillLimits(timeout_s=5.0, max_output_chars=8_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    def __init__(self, repo: Repository, *, window_days: int, minimum_threads: int) -> None:
        self._repo = repo
        self._window_days = window_days
        self._minimum_threads = minimum_threads

    async def execute(self, context: SkillContext, payload: InsightsInput) -> InsightsOutput:
        result = await compute_insights(
            self._repo,
            guild_id=str(context.guild_id),
            window_days=self._window_days,
            minimum_threads=self._minimum_threads,
        )
        return InsightsOutput(
            window_days=result.window_days,
            minimum_threads=result.minimum_threads,
            total_threads=result.total_threads,
            sufficient=result.sufficient,
            status_counts=result.status_counts,
            top_errors=result.top_errors,
            threads_with_open_questions=result.threads_with_open_questions,
        )


class PulseInput(SkillInput):
    """No caller input; guild, window and threshold come from trusted config."""


class PulseOutput(SkillOutput):
    pulse: CommunityPulse


class PulseSkill(Skill[PulseInput, PulseOutput]):
    """Expose the deterministic, minimum-data-gated Community Pulse read-only."""

    skill_id = "pulse.community"
    description = "Return an evidence-backed, guild-scoped Community Pulse snapshot."
    input_model = PulseInput
    output_model = PulseOutput
    permissions = frozenset({SkillPermission.moderator})
    limits = SkillLimits(timeout_s=10.0, max_output_chars=20_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    def __init__(self, repo: Repository, *, window_days: int, minimum_threads: int) -> None:
        self._repo = repo
        self._window_days = window_days
        self._minimum_threads = minimum_threads

    async def execute(self, context: SkillContext, payload: PulseInput) -> PulseOutput:
        pulse = await compute_community_pulse(
            self._repo,
            guild_id=str(context.guild_id),
            window_days=self._window_days,
            minimum_threads=self._minimum_threads,
        )
        references: list[Reference] = []
        seen: set[tuple[str, str]] = set()
        for observation in pulse.observations:
            for item in observation.evidence:
                key = (item.kind, item.ref_id)
                if key in seen:
                    continue
                seen.add(key)
                references.append(
                    Reference(
                        kind=truncate(item.kind, 32) or "evidence",
                        title=truncate(sanitize_for_prompt(item.title), 200) or "Evidence",
                        url=truncate(item.url, 1000) if item.url else None,
                        snippet=truncate(sanitize_for_prompt(item.snippet), 400),
                    )
                )
                if len(references) >= 20:
                    break
            if len(references) >= 20:
                break
        return PulseOutput(pulse=pulse, references=references)


class CandidateSummaryOutput(SkillOutput):
    candidate_id: int
    state: str
    source_kind: str
    source_summary_id: int | None
    stale: bool
    excerpt: str
    finding_codes: list[str]


class CandidatesInput(SkillInput):
    limit: int = Field(default=10, ge=1, le=20)


class CandidatesOutput(SkillOutput):
    candidates: list[CandidateSummaryOutput]


class KnowledgeCandidatesSkill(Skill[CandidatesInput, CandidatesOutput]):
    """List unapproved candidates for moderators; creation/review stays human."""

    skill_id = "knowledge.candidates"
    description = "List guild-scoped knowledge candidates awaiting human review."
    input_model = CandidatesInput
    output_model = CandidatesOutput
    permissions = frozenset({SkillPermission.moderator})
    limits = SkillLimits(timeout_s=5.0, max_output_chars=12_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def execute(self, context: SkillContext, payload: CandidatesInput) -> CandidatesOutput:
        # The skill context is already an application-authorization boundary:
        # moderator is the only permission that can reach this skill. Projecting
        # it onto the review actor does not grant anything new.
        actor = ApprovalActor(
            actor_id=str(context.user_id),
            guild_id=int(context.guild_id),
            kind=ActorKind.human,
            permissions=frozenset({ApprovalPermission.propose, ApprovalPermission.decide}),
        )
        views = await list_candidates(actor, self._repo, state="proposed", limit=payload.limit)
        summaries = [
            CandidateSummaryOutput(
                candidate_id=view.candidate_id,
                state=view.state,
                source_kind=view.source_kind,
                source_summary_id=view.source_summary_id,
                stale=view.stale,
                excerpt=truncate(sanitize_for_prompt(view.body), 280),
                finding_codes=[item.code for item in view.findings],
            )
            for view in views
        ]
        return CandidatesOutput(candidates=summaries)


class ResearchInput(SkillInput):
    query: str = Field(min_length=3, max_length=500)


class ResearchOutput(SkillOutput):
    query: str
    status: str
    provider: str
    synthesis: str
    summary: str
    citations: list[str]


class ResearchSkill(Skill[ResearchInput, ResearchOutput]):
    """One bounded web search + extractive summary through the research service.

    The mission runner calls this without model synthesis so the skill stays
    within its timeout. The interactive /research command can add synthesis.
    """

    skill_id = "research.web"
    description = "Run one bounded web search and return grounded, cited sources."
    input_model = ResearchInput
    output_model = ResearchOutput
    permissions = frozenset({SkillPermission.moderator})
    limits = SkillLimits(timeout_s=30.0, max_output_chars=18_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    def __init__(self, service: ResearchService) -> None:
        self._service = service

    async def execute(self, context: SkillContext, payload: ResearchInput) -> ResearchOutput:
        outcome = await self._service.research(
            guild_id=str(context.guild_id),
            query=payload.query,
            llm=None,
        )
        if outcome.status is ResearchStatus.unavailable:
            raise ResearchUnavailableError(outcome.reason)
        if outcome.status is not ResearchStatus.completed:
            if outcome.error_code == "no_results":
                raise ResearchNoResultsError(outcome.reason)
            raise ResearchProviderError(outcome.reason)
        return ResearchOutput(
            query=outcome.query,
            status=outcome.status.value,
            provider=outcome.provider,
            synthesis=outcome.synthesis,
            summary=outcome.summary,
            citations=outcome.citation_lines(),
            references=[
                Reference(
                    kind="web",
                    title=truncate(source.title, 200) or "Web source",
                    url=truncate(source.url, 1000),
                    snippet=truncate(source.snippet, 400),
                )
                for source in outcome.sources[:20]
            ],
        )


class SynthesisInput(SkillInput):
    """Goal plus the bounded outputs of dependency steps, injected by the host."""

    goal: str = Field(min_length=3, max_length=500)
    upstream: dict[str, Any] = Field(default_factory=dict)


class SynthesisOutput(SkillOutput):
    brief: str
    sources_used: int


class SynthesisBriefSkill(Skill[SynthesisInput, SynthesisOutput]):
    """Deterministic combination of dependency outputs; no model call, no invented facts."""

    skill_id = "synthesis.brief"
    description = "Combine upstream read-only results into one traceable brief."
    input_model = SynthesisInput
    output_model = SynthesisOutput
    permissions = frozenset({SkillPermission.guild_member})
    limits = SkillLimits(timeout_s=5.0, max_output_chars=12_000)
    read_only = True
    error_policy = SkillErrorPolicy.safe_error

    async def execute(self, context: SkillContext, payload: SynthesisInput) -> SynthesisOutput:
        lines = [f"Goal: {truncate(sanitize_for_prompt(payload.goal), 500)}", "", "Evidence reviewed:"]
        sources = 0
        uncertainty: list[str] = []
        for key, value in list(payload.upstream.items())[:6]:
            label = truncate(sanitize_for_prompt(str(key)), 80) or "step"
            text = _summarize_upstream(value)
            if not text:
                uncertainty.append(f"{label}: no usable output was returned.")
                continue
            sources += 1
            lines.append(f"- {label}: {text}")
        if not sources:
            uncertainty.append("No dependency returned usable evidence.")
        lines.append("")
        lines.append("What is observed:")
        if sources:
            lines.append("- The statements above come from the listed read-only tool outputs only.")
        else:
            lines.append("- Nothing verifiable was retrieved for this goal.")
        lines.append("")
        lines.append("Uncertainty / conflicts:")
        if uncertainty:
            lines.extend(f"- {item}" for item in uncertainty)
        else:
            lines.append(
                "- No direct conflict was detected in the returned evidence; "
                "absence of conflict is not proof."
            )
        lines.append("")
        lines.append(
            "This brief is read-only evidence combination, not a verified real-world outcome."
        )
        return SynthesisOutput(brief=truncate("\n".join(lines), 8_000), sources_used=sources)


def _summarize_upstream(value: object) -> str:
    """Compact one upstream skill output without trusting or inventing its fields."""
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for field in ("summary", "brief", "text", "status", "query"):
        item = value.get(field)
        if isinstance(item, str) and item.strip():
            parts.append(f"{field}={truncate(sanitize_for_prompt(item), 700)}")
    references = value.get("references")
    if isinstance(references, list) and references:
        titles: list[str] = []
        for reference in references[:5]:
            if isinstance(reference, dict):
                title = reference.get("title")
                if isinstance(title, str) and title.strip():
                    titles.append(truncate(sanitize_for_prompt(title), 120))
        if titles:
            parts.append("references=" + "; ".join(titles))
    if not parts:
        # Structured outputs without the common fields (pulse/insights): a bounded
        # JSON projection is honest and deterministic; it is labelled as raw data.
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return ""
        parts.append("data=" + truncate(sanitize_for_prompt(raw), 1_200))
    return " | ".join(parts)


def build_default_registry(
    repo: Repository,
    *,
    insights_window_days: int,
    insights_minimum_threads: int,
    pulse_window_days: int | None = None,
    pulse_minimum_threads: int | None = None,
    research_service: ResearchService | None = None,
) -> SkillRegistry:
    """Build the explicit registry (read-only capabilities only)."""
    registry = SkillRegistry()
    registry.register(SynthesisBriefSkill())
    registry.register(KbSearchSkill(repo))
    registry.register(
        InsightsSkill(
            repo,
            window_days=insights_window_days,
            minimum_threads=insights_minimum_threads,
        )
    )
    registry.register(
        PulseSkill(
            repo,
            window_days=pulse_window_days or insights_window_days,
            minimum_threads=pulse_minimum_threads or insights_minimum_threads,
        )
    )
    registry.register(KnowledgeCandidatesSkill(repo))
    if research_service is not None:
        registry.register(ResearchSkill(research_service))
    return registry
