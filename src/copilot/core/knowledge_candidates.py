"""Reviewable lifecycle for knowledge-base solution candidates.

This module does not invent a second approval system. A candidate is the
existing ``solutions`` row in state ``proposed``; publishing it means the
existing ``solutions.approve_solution`` path, which atomically flips the state
and inserts the approved ``kb_docs`` row. Unapproved candidates therefore stay
outside every approved-only retrieval path (chat grounding, ``kb.search``).

Trusted application code supplies the actor and its permissions. Model output
never decides who may propose, review, or publish. Duplicate/contradiction
findings are deterministic lexical signals, not semantic verdicts; the labels
say so explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..errors import (
    ApprovalDeniedError,
    KnowledgeCandidateConflictError,
    KnowledgeCandidateDuplicateError,
    KnowledgeCandidateNotFoundError,
    KnowledgeCandidateSourceError,
    KnowledgeCandidateStaleError,
)
from .approvals import ActorKind, ApprovalActor, ApprovalPermission
from .solutions import approve_solution, reject_solution

MAX_CANDIDATE_BODY_CHARS = 4000
MAX_FINDINGS = 5
_SIMILARITY_THRESHOLD = 0.6
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = frozenset(
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
        "from",
        "have",
        "into",
        "just",
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

FindingCode = Literal[
    "duplicate",
    "possible_duplicate",
    "possible_contradiction",
    "related_approved_knowledge",
    "stale_source",
]


class CandidateFinding(BaseModel):
    """A bounded, reviewable signal; never an automatic decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: FindingCode
    message: str = Field(min_length=1, max_length=300)
    candidate_id: int | None = None
    thread_row_id: int | None = None
    kb_doc_id: int | None = None


class CandidateView(BaseModel):
    """One guild-scoped candidate plus review state and lexical findings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: int
    guild_id: str
    thread_row_id: int
    discord_thread_id: str
    thread_name: str
    body: str
    source_url: str | None
    state: str
    source_kind: str
    source_summary_id: int | None
    content_hash: str | None
    proposed_by: str
    proposed_at: str
    decided_by: str | None
    decided_at: str | None
    stale: bool
    findings: list[CandidateFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)


class CandidateProposal(BaseModel):
    """A discovered source text that has NOT been stored yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guild_id: str
    thread_row_id: int
    discord_thread_id: str
    thread_name: str
    source_summary_id: int
    body: str
    rationale: str


def content_digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall((text or "").lower()) if token not in _STOPWORDS}


def _overlap(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _require_human(actor: ApprovalActor, permission: ApprovalPermission) -> None:
    if actor.kind is not ActorKind.human or permission not in actor.permissions:
        raise ApprovalDeniedError()


def _guild(actor: ApprovalActor) -> str:
    return str(actor.guild_id)


def _load_analysis(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _is_stale(repo: Repository, row: Any) -> bool:
    source_summary_id = row["source_summary_id"]
    if source_summary_id is None:
        return False
    latest = await repo.latest_summary(int(row["thread_id"]))
    return latest is None or int(latest["id"]) != int(source_summary_id)


async def candidate_findings(
    repo: Repository,
    *,
    guild_id: str,
    thread_row_id: int,
    body: str,
    content_hash: str | None = None,
    exclude_candidate_id: int | None = None,
) -> list[CandidateFinding]:
    """Deterministic duplicate/contradiction hints for human review.

    - an identical body hash is a hard duplicate signal;
    - high lexical overlap (Jaccard >= 0.6) on the same thread is labeled a
      *possible contradiction*, and on another thread a *possible duplicate*;
    - approved KB matches are listed as related knowledge, not as proof that the
      candidate adds nothing.
    """
    digest = content_hash or content_digest(body)
    findings: list[CandidateFinding] = []
    rows = await repo.solutions_for_guild(guild_id=guild_id, limit=200)

    for row in rows:
        if exclude_candidate_id is not None and int(row["id"]) == int(exclude_candidate_id):
            continue
        if row["state"] == "rejected":
            continue
        if row["content_hash"] and str(row["content_hash"]) == digest:
            findings.append(
                CandidateFinding(
                    code="duplicate",
                    message=(
                        "An identical candidate body already exists in this server "
                        f"(candidate {int(row['id'])}, state {row['state']})."
                    ),
                    candidate_id=int(row["id"]),
                    thread_row_id=int(row["thread_id"]),
                )
            )
            break

    if not any(item.code == "duplicate" for item in findings):
        scored: list[tuple[float, Any]] = []
        for row in rows:
            if exclude_candidate_id is not None and int(row["id"]) == int(exclude_candidate_id):
                continue
            if row["state"] == "rejected":
                continue
            score = _overlap(body, str(row["body"] or ""))
            if score >= _SIMILARITY_THRESHOLD:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], int(item[1]["id"])))
        if scored:
            _score, row = scored[0]
            same_thread = int(row["thread_id"]) == int(thread_row_id)
            findings.append(
                CandidateFinding(
                    code="possible_contradiction" if same_thread else "possible_duplicate",
                    message=(
                        "High lexical overlap with candidate "
                        f"{int(row['id'])} ({row['state']}). This is a wording signal, "
                        "not a verified contradiction or equivalence; review both."
                    ),
                    candidate_id=int(row["id"]),
                    thread_row_id=int(row["thread_id"]),
                )
            )

    approved = await repo.search_kb(body, guild_id=guild_id, approved_only=True, limit=1)
    if approved:
        findings.append(
            CandidateFinding(
                code="related_approved_knowledge",
                message=(
                    "Approved knowledge may already cover this topic; verify before "
                    "publishing a second answer. Lexical BM25 match only."
                ),
                kb_doc_id=int(approved[0]["id"]),
            )
        )

    return findings[:MAX_FINDINGS]


def _view(row: Any, *, stale: bool, findings: Sequence[CandidateFinding]) -> CandidateView:
    return CandidateView(
        candidate_id=int(row["id"]),
        guild_id=str(row["thread_guild_id"] or ""),
        thread_row_id=int(row["thread_id"]),
        discord_thread_id=str(row["discord_thread_id"] or ""),
        thread_name=str(row["thread_name"] or ""),
        body=str(row["body"]),
        source_url=row["source_url"],
        state=str(row["state"]),
        source_kind=str(row["source_kind"] or "manual"),
        source_summary_id=(
            int(row["source_summary_id"]) if row["source_summary_id"] is not None else None
        ),
        content_hash=row["content_hash"],
        proposed_by=str(row["proposed_by"]),
        proposed_at=str(row["proposed_at"]),
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        stale=stale,
        findings=list(findings)[:MAX_FINDINGS],
    )


async def _load_candidate_view(
    repo: Repository,
    candidate_id: int,
    *,
    guild_id: str,
    findings: Sequence[CandidateFinding] = (),
) -> CandidateView:
    row = await repo.get_candidate_for_guild(candidate_id, guild_id=guild_id)
    if row is None:
        raise KnowledgeCandidateNotFoundError()
    return _view(row, stale=await _is_stale(repo, row), findings=findings)


async def discover_from_summary(
    repo: Repository, *, guild_id: str, summary_id: int
) -> CandidateProposal:
    """Extract a candidate proposal from one guild-owned summary (no write)."""
    summary = await repo.get_summary_for_guild(summary_id, guild_id=str(guild_id))
    if summary is None:
        raise KnowledgeCandidateSourceError()

    analysis = _load_analysis(str(summary["analysis_json"] or "{}"))
    body = ""
    attempted = analysis.get("attempted_solutions")
    if isinstance(attempted, list):
        for item in attempted:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                body = item["text"].strip()
                break
    if not body and isinstance(analysis.get("suggested_next_step"), str):
        body = analysis["suggested_next_step"].strip()
    if not body:
        decisions = analysis.get("decisions")
        if isinstance(decisions, list):
            for item in decisions:
                if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                    body = item["text"].strip()
                    break
    if not body:
        raise KnowledgeCandidateSourceError(detail="The summary contains no candidate text.")
    if len(body) > MAX_CANDIDATE_BODY_CHARS:
        body = body[:MAX_CANDIDATE_BODY_CHARS]

    return CandidateProposal(
        guild_id=str(guild_id),
        thread_row_id=int(summary["thread_id"]),
        discord_thread_id=str(summary["discord_thread_id"] or ""),
        thread_name=str(summary["thread_name"] or ""),
        source_summary_id=int(summary["id"]),
        body=body,
        rationale=(
            f"Extracted from summary {int(summary['id'])}. It remains unapproved until a "
            "moderator reviews the exact text and the source is still current."
        ),
    )


async def propose_candidate(
    actor: ApprovalActor,
    repo: Repository,
    *,
    thread_row_id: int,
    body: str,
    source_url: str | None = None,
    source_summary_id: int | None = None,
    source_kind: Literal["manual", "summary", "pulse"] = "manual",
) -> CandidateView:
    """Store one unapproved candidate after validating its source and guild."""
    _require_human(actor, ApprovalPermission.propose)

    text = (body or "").strip()
    if not text:
        raise KnowledgeCandidateSourceError(detail="Candidate body is empty.")
    if len(text) > MAX_CANDIDATE_BODY_CHARS:
        raise KnowledgeCandidateSourceError(
            detail=f"Candidate body exceeds {MAX_CANDIDATE_BODY_CHARS} characters."
        )

    thread = await repo.get_thread_row(int(thread_row_id))
    if thread is None or str(thread["guild_id"]) != _guild(actor):
        raise KnowledgeCandidateSourceError(detail="Thread is missing or belongs to another guild.")

    if source_summary_id is not None:
        summary = await repo.get_summary_for_guild(int(source_summary_id), guild_id=_guild(actor))
        if summary is None or int(summary["thread_id"]) != int(thread_row_id):
            raise KnowledgeCandidateSourceError(
                detail="Summary is missing, belongs to another guild, or points at another thread."
            )
    elif source_kind == "summary":
        raise KnowledgeCandidateSourceError(detail="A summary source requires source_summary_id.")

    if source_kind not in {"manual", "summary", "pulse"}:
        raise KnowledgeCandidateSourceError(detail="Unknown candidate source kind.")

    digest = content_digest(text)
    findings = await candidate_findings(
        repo,
        guild_id=_guild(actor),
        thread_row_id=int(thread_row_id),
        body=text,
        content_hash=digest,
    )
    duplicate = next((item for item in findings if item.code == "duplicate"), None)
    if duplicate is not None:
        raise KnowledgeCandidateDuplicateError(
            detail=f"existing_candidate_id={duplicate.candidate_id}"
        )

    candidate_id = await repo.insert_solution(
        thread_id=int(thread_row_id),
        body=text,
        source_url=source_url,
        actor_id=actor.actor_id,
        content_hash=digest,
        source_summary_id=int(source_summary_id) if source_summary_id is not None else None,
        source_kind=source_kind,
    )
    await repo.audit(
        actor_id=actor.actor_id,
        action="knowledge.candidate.proposed",
        target_type="solution",
        target_id=str(candidate_id),
        guild_id=_guild(actor),
        meta={
            "guild_id": _guild(actor),
            "thread_row_id": int(thread_row_id),
            "source_kind": source_kind,
            "source_summary_id": source_summary_id,
            "content_hash": digest,
            "findings": [item.code for item in findings],
        },
    )
    return await _load_candidate_view(
        repo, candidate_id, guild_id=_guild(actor), findings=findings
    )


async def accept_proposal(
    actor: ApprovalActor, repo: Repository, proposal: CandidateProposal
) -> CandidateView:
    """Accept a discovered proposal as an unapproved candidate."""
    if str(proposal.guild_id) != _guild(actor):
        raise KnowledgeCandidateSourceError(detail="Proposal belongs to another guild.")
    return await propose_candidate(
        actor,
        repo,
        thread_row_id=proposal.thread_row_id,
        body=proposal.body,
        source_summary_id=proposal.source_summary_id,
        source_kind="summary",
    )


async def list_candidates(
    actor: ApprovalActor,
    repo: Repository,
    *,
    state: str | None = "proposed",
    limit: int = 50,
) -> list[CandidateView]:
    """List guild-owned candidates and their current staleness (read-only)."""
    _require_human(actor, ApprovalPermission.decide)
    rows = await repo.solutions_for_guild(
        guild_id=_guild(actor), state=state, limit=max(1, min(int(limit), 100))
    )
    views: list[CandidateView] = []
    for row in rows:
        views.append(_view(row, stale=await _is_stale(repo, row), findings=[]))
    return views


async def review_candidate(
    actor: ApprovalActor,
    repo: Repository,
    *,
    candidate_id: int,
    decision: Literal["approve", "reject"],
    note: str | None = None,
) -> CandidateView:
    """Approve/reject one candidate through the existing solution lifecycle.

    The state transition is conditional on ``state = 'proposed'`` inside the
    repository transaction, so two concurrent reviewers cannot both win. An
    approved candidate is inserted as a new ``kb_docs`` row; an existing
    approved solution is never updated or overwritten.
    """
    _require_human(actor, ApprovalPermission.decide)

    row = await repo.get_candidate_for_guild(int(candidate_id), guild_id=_guild(actor))
    if row is None:
        raise KnowledgeCandidateNotFoundError()
    if row["state"] != "proposed":
        raise KnowledgeCandidateConflictError(detail=f"already_{row['state']}")

    if decision == "approve":
        ok, outcome = await approve_solution(
            repo, solution_id=int(candidate_id), guild_id=_guild(actor), actor_id=actor.actor_id
        )
        if not ok:
            if outcome == "stale":
                raise KnowledgeCandidateStaleError()
            if outcome == "not_found":
                raise KnowledgeCandidateNotFoundError()
            raise KnowledgeCandidateConflictError(detail=outcome)
    else:
        ok, outcome = await reject_solution(
            repo,
            solution_id=int(candidate_id),
            guild_id=_guild(actor),
            actor_id=actor.actor_id,
            note=note,
        )
        if not ok:
            if outcome == "not_found":
                raise KnowledgeCandidateNotFoundError()
            raise KnowledgeCandidateConflictError(detail=outcome)

    await repo.audit(
        actor_id=actor.actor_id,
        action=f"knowledge.candidate.{decision}d",
        target_type="solution",
        target_id=str(candidate_id),
        guild_id=_guild(actor),
        meta={"guild_id": _guild(actor)},
    )
    return await _load_candidate_view(repo, int(candidate_id), guild_id=_guild(actor))

