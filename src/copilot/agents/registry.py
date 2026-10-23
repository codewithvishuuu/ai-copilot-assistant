"""Small explicit skill registry.

Only validated ``Skill`` instances with a stable ID can enter the registry.
There is no decorator that turns an arbitrary function into a capability.
"""

from __future__ import annotations

from typing import Any

from .contract import (
    MAX_SKILL_OUTPUT_CHARS,
    MAX_SKILL_TIMEOUT_S,
    SKILL_ID_RE,
    Skill,
    SkillErrorPolicy,
    SkillInput,
    SkillLimits,
    SkillOutput,
    SkillPermission,
)


class SkillRegistry:
    """Insertion-order registry with duplicate and metadata rejection."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill[Any, Any]] = {}

    def register(self, skill: Skill[Any, Any]) -> None:
        """Register one skill or raise ``TypeError``/``ValueError``.

        Validation happens at registration time so malformed metadata can never
        reach the orchestrator at request time.
        """
        if not isinstance(skill, Skill):
            raise TypeError("Only Skill subclasses may be registered; arbitrary callables are rejected.")

        skill_id = getattr(skill, "skill_id", None)
        if not isinstance(skill_id, str) or not SKILL_ID_RE.fullmatch(skill_id):
            raise ValueError("Skill IDs must match ^[a-z][a-z0-9_.-]{1,63}$.")
        if skill_id in self._skills:
            raise ValueError(f"Skill ID {skill_id!r} is already registered.")

        description = getattr(skill, "description", None)
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise ValueError(f"Skill {skill_id!r} must have a non-empty description of at most 500 chars.")

        input_model = getattr(skill, "input_model", None)
        if not isinstance(input_model, type) or not issubclass(input_model, SkillInput):
            raise ValueError(f"Skill {skill_id!r} input_model must be a SkillInput subclass.")
        output_model = getattr(skill, "output_model", None)
        if not isinstance(output_model, type) or not issubclass(output_model, SkillOutput):
            raise ValueError(f"Skill {skill_id!r} output_model must be a SkillOutput subclass.")

        permissions = getattr(skill, "permissions", None)
        if not isinstance(permissions, (set, frozenset)) or not permissions:
            raise ValueError(f"Skill {skill_id!r} must declare at least one permission.")
        if any(not isinstance(permission, SkillPermission) for permission in permissions):
            raise ValueError(f"Skill {skill_id!r} declares an unknown permission.")

        limits = getattr(skill, "limits", None)
        if not isinstance(limits, SkillLimits):
            raise ValueError(f"Skill {skill_id!r} must declare SkillLimits.")
        if not (0.0 < limits.timeout_s <= MAX_SKILL_TIMEOUT_S):
            raise ValueError(f"Skill {skill_id!r} timeout must be in (0, {MAX_SKILL_TIMEOUT_S}] seconds.")
        if not (0 < limits.max_output_chars <= MAX_SKILL_OUTPUT_CHARS):
            raise ValueError(f"Skill {skill_id!r} max_output_chars must be in (0, {MAX_SKILL_OUTPUT_CHARS}].")

        if getattr(skill, "read_only", None) is not True:
            raise ValueError(f"Skill {skill_id!r} is not read-only; Phase 1 accepts no public actions.")
        if getattr(skill, "error_policy", None) is not SkillErrorPolicy.safe_error:
            raise ValueError(f"Skill {skill_id!r} must use the safe_error policy.")

        self._skills[skill_id] = skill

    def get(self, skill_id: str) -> Skill[Any, Any] | None:
        """Return a registered skill, or ``None`` for an unknown ID."""
        return self._skills.get(skill_id)

    def require(self, skill_id: str) -> Skill[Any, Any]:
        """Return a registered skill or raise ``KeyError``."""
        skill = self.get(skill_id)
        if skill is None:
            raise KeyError(skill_id)
        return skill

    @property
    def ids(self) -> tuple[str, ...]:
        """Registered IDs in deterministic order."""
        return tuple(sorted(self._skills))

    def all(self) -> tuple[Skill[Any, Any], ...]:
        """Registered skills in deterministic ID order."""
        return tuple(self._skills[skill_id] for skill_id in self.ids)

    def __len__(self) -> int:
        return len(self._skills)
