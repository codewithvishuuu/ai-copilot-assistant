"""Deterministic mission plans over the registered read-only skills.

The model never chooses skills or tools: a mode maps to a fixed, ordered list
of specialist steps, filtered by what is actually registered. This is deliberate
(no model-controlled tool surface); "specialist delegation" means each step is a
typed application skill with its own input contract, timeout and permissions.
"""

from __future__ import annotations

from typing import Any

from ..errors import MissionPlanError
from ..util.text import strip_control_tokens, truncate
from .models import MissionMode, MissionPlan, PlannedStep

STEP_TITLES = {
    "pulse.community": "Community pulse evidence",
    "insights.community": "Community insights aggregate",
    "kb.search": "Approved knowledge search",
    "research.web": "Live web research",
}

_MODE_REQUIRED: dict[MissionMode, tuple[str, ...]] = {
    MissionMode.community: ("pulse.community", "insights.community", "kb.search"),
    MissionMode.research: ("research.web", "kb.search"),
    MissionMode.draft_prep: ("kb.search", "pulse.community"),
}
_MODE_OPTIONAL: dict[MissionMode, tuple[str, ...]] = {
    MissionMode.community: (),
    MissionMode.research: (),
    MissionMode.draft_prep: ("research.web",),
}


def _input_for(skill_id: str, goal: str) -> dict[str, Any]:
    if skill_id == "kb.search":
        return {"query": truncate(goal, 200), "limit": 5}
    if skill_id == "research.web":
        return {"query": truncate(goal, 500)}
    return {}


def build_mission_plan(
    *,
    goal: str,
    mode: MissionMode,
    settings: Any,
    available_skills: set[str],
) -> MissionPlan:
    """Build one bounded plan or raise ``MissionPlanError`` before anything is stored."""
    clean_goal = " ".join(strip_control_tokens(goal or "").split())
    if len(clean_goal) < 3:
        raise MissionPlanError("Describe the mission goal in at least three characters.")

    required = _MODE_REQUIRED.get(mode, ())
    missing = [skill_id for skill_id in required if skill_id not in available_skills]
    if missing:
        raise MissionPlanError(
            f"This mission mode needs unavailable capabilities: {', '.join(missing)}. "
            "Check Mission Control for provider status or choose another mode."
        )

    skill_ids = list(required)
    for skill_id in _MODE_OPTIONAL.get(mode, ()):
        if skill_id in available_skills and skill_id not in skill_ids:
            skill_ids.append(skill_id)

    max_steps = int(getattr(settings, "mission_max_steps", 4))
    steps = [
        PlannedStep(
            index=index,
            title=STEP_TITLES.get(skill_id, skill_id),
            skill_id=skill_id,
            input=_input_for(skill_id, clean_goal),
        )
        for index, skill_id in enumerate(skill_ids[:max_steps])
    ]
    if not steps:
        raise MissionPlanError("No mission step could be planned.")
    return MissionPlan(mode=mode, steps=steps)
