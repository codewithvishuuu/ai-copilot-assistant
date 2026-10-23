"""Problem Radar: recurring issues, knowledge gaps and evidence in one view.

The Radar is a read model over Community Pulse, not a second inference system.
It promotes recurring question/problem observations and explicit knowledge gaps
into typed entries with a confidence label derived from observed counts. Every
entry keeps its reproducible evidence; recommendations keep Pulse's uncertainty
wording. No member profiling, no causal claims and no writes happen here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..util.text import truncate
from .community_pulse import (
    PulseEvidence,
    PulseObservation,
    PulseRecommendation,
    compute_community_pulse,
)

_MAX_PROBLEMS = 8
_MAX_GAPS = 8
_PROBLEM_KINDS = {
    "repeated_question": "Repeated question",
    "recurring_problem": "Recurring problem",
    "unresolved_threads": "Open threads",
}
_UNCERTAINTY = {
    "repeated_question": (
        "Grouped by normalized question text across distinct summarized threads; "
        "a shared wording does not prove a shared root cause."
    ),
    "recurring_problem": (
        "Grouped by normalized error signature across distinct summarized threads; "
        "different causes can produce similar messages."
    ),
    "unresolved_threads": (
        "Based on stored summary status only. A thread may have been resolved without "
        "being re-summarized; verify before acting."
    ),
}


class RadarProblem(BaseModel):
    """One recurring, evidence-backed issue with explicit uncertainty."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    problem_id: str = Field(min_length=8, max_length=40)
    kind: Literal["repeated_question", "recurring_problem", "unresolved_threads"]
    title: str = Field(min_length=1, max_length=300)
    count: int = Field(ge=0)
    confidence: Literal["low", "medium", "high"]
    evidence: list[PulseEvidence] = Field(default_factory=list, max_length=5)
    uncertainty: str = Field(max_length=500)
    kb_status: Literal["covered", "missing", "not_checked"] = "not_checked"
    suggested_action: str = Field(default="", max_length=800)


class RadarGap(BaseModel):
    """A recurring topic with no approved knowledge-base match."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_id: str = Field(min_length=8, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    count: int = Field(ge=1)
    confidence: Literal["low", "medium", "high"]
    evidence: list[PulseEvidence] = Field(default_factory=list, max_length=5)
    note: str = Field(default="", max_length=500)
    suggested_action: str = Field(default="", max_length=800)


class RadarSnapshot(BaseModel):
    """One guild-scoped Radar computation; safe to display as evidence."""

    model_config = ConfigDict(extra="forbid")

    window_days: int
    minimum_threads: int
    total_threads: int
    sufficient: bool
    problems: list[RadarProblem] = Field(default_factory=list)
    gaps: list[RadarGap] = Field(default_factory=list)
    recommendations: list[PulseRecommendation] = Field(default_factory=list, max_length=8)
    coverage_summary: str = ""
    notes: list[str] = Field(default_factory=list)


def _digest(kind: str, title: str) -> str:
    return f"{kind[:8]}-" + hashlib.sha1(f"{kind}:{title}".encode()).hexdigest()[:12]


def _confidence(count: int) -> Literal["low", "medium", "high"]:
    if count >= 5:
        return "high"
    if count >= 3:
        return "medium"
    return "low"


def _action_for(label: str, recommendations: list[PulseRecommendation]) -> str:
    lowered = label.lower()
    for recommendation in recommendations:
        if lowered in recommendation.title.lower():
            return recommendation.suggested_action
    return ""


def _problem_from(
    observation: PulseObservation, recommendations: list[PulseRecommendation]
) -> RadarProblem | None:
    if observation.kind not in _PROBLEM_KINDS:
        return None
    return RadarProblem(
        problem_id=_digest(observation.kind, observation.title),
        kind=observation.kind,  # type: ignore[arg-type]
        title=truncate(observation.title, 300),
        count=observation.count,
        confidence=_confidence(observation.count),
        evidence=list(observation.evidence),
        uncertainty=_UNCERTAINTY[observation.kind],
        kb_status="not_checked",
        suggested_action=_action_for(observation.title, recommendations),
    )


def _gap_from(
    observation: PulseObservation, recommendations: list[PulseRecommendation]
) -> RadarGap | None:
    if observation.kind != "knowledge_gap":
        return None
    return RadarGap(
        gap_id=_digest("gap", observation.title),
        title=truncate(observation.title, 300),
        count=max(1, observation.count),
        confidence=_confidence(max(1, observation.count)),
        evidence=list(observation.evidence),
        note=truncate(observation.note, 500),
        suggested_action=_action_for(observation.title, recommendations),
    )


async def compute_problem_radar(
    repo: Repository,
    *,
    guild_id: str,
    window_days: int,
    minimum_threads: int,
    clock: Callable[[], datetime] | None = None,
) -> RadarSnapshot:
    """Build the Radar from the existing minimum-data-gated Pulse computation."""
    pulse = await compute_community_pulse(
        repo,
        guild_id=str(guild_id),
        window_days=window_days,
        minimum_threads=minimum_threads,
        clock=clock,
    )
    if not pulse.sufficient:
        return RadarSnapshot(
            window_days=window_days,
            minimum_threads=minimum_threads,
            total_threads=pulse.total_threads,
            sufficient=False,
            coverage_summary="No Radar evidence yet; insights unlock with more summarized threads.",
            notes=["The Radar only reads stored summaries; it never scrapes live channels."],
        )

    recommendations = list(pulse.recommendations)
    problems: list[RadarProblem] = []
    gaps: list[RadarGap] = []
    seen_problems: set[tuple[str, str]] = set()
    seen_gaps: set[str] = set()
    gap_titles = {
        observation.title.lower()
        for observation in pulse.observations
        if observation.kind == "knowledge_gap"
    }

    for observation in pulse.observations:
        problem = _problem_from(observation, recommendations)
        if problem is not None:
            key = (problem.kind, problem.title.lower())
            if key in seen_problems:
                continue
            seen_problems.add(key)
            if problem.title.lower() in gap_titles:
                problem = problem.model_copy(update={"kb_status": "missing"})
            problems.append(problem)
        gap = _gap_from(observation, recommendations)
        if gap is not None and gap.title.lower() not in seen_gaps:
            seen_gaps.add(gap.title.lower())
            gaps.append(gap)

    problems.sort(key=lambda item: (-item.count, item.title.lower()))
    gaps.sort(key=lambda item: (-item.count, item.title.lower()))
    problems = problems[:_MAX_PROBLEMS]
    gaps = gaps[:_MAX_GAPS]

    if gaps:
        coverage = (
            f"{len(gaps)} recurring topic(s) have no approved knowledge-base match. "
            "Approved knowledge is the only coverage signal the Radar can verify."
        )
    elif problems:
        coverage = (
            "No uncovered recurring topic was detected in this window; "
            "approved knowledge may still be stale and should be reviewed."
        )
    else:
        coverage = "No recurring question or error cluster reached the minimum repeat count."

    return RadarSnapshot(
        window_days=window_days,
        minimum_threads=minimum_threads,
        total_threads=pulse.total_threads,
        sufficient=True,
        problems=problems,
        gaps=gaps,
        recommendations=recommendations,
        coverage_summary=coverage,
        notes=[
            "Evidence is limited to stored, guild-scoped summaries and approved knowledge.",
            "Confidence reflects the observed repeat count, not a statistical measurement.",
            "Recommendations are estimates; a moderator must review them. Nothing is automatic.",
        ],
    )
