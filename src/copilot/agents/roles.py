"""Specialist role registry for the Main Host Agent.

Roles are declarative capability boundaries, not separate process identities.
A role names the read-only skills it may use, the permissions those skills
require, and whether any human-gated action is involved. The host plans only
with roles whose skills are actually registered; unavailable roles are reported
honestly instead of being simulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contract import SkillPermission


class RoleId(str, Enum):
    community_manager = "community_manager"
    web_researcher = "web_researcher"
    content_writer = "content_writer"
    community_analytics = "community_analytics"
    knowledge_memory = "knowledge_memory"
    ai_moderation = "ai_moderation"
    vision_troubleshooting = "vision_troubleshooting"
    quality_evaluation = "quality_evaluation"


@dataclass(frozen=True)
class SpecialistRole:
    role_id: RoleId
    title: str
    purpose: str
    capabilities: tuple[str, ...]
    skill_ids: tuple[str, ...]
    permissions: frozenset[SkillPermission]
    writes_public_content: bool = False
    requires_human_approval: bool = False
    implementation_note: str = ""


ROLE_DEFINITIONS: tuple[SpecialistRole, ...] = (
    SpecialistRole(
        role_id=RoleId.community_manager,
        title="Community Manager",
        purpose="Understand the goal, plan bounded work, delegate, combine and explain results.",
        capabilities=(
            "goal interpretation",
            "bounded planning",
            "delegation",
            "result combination",
            "conflict and uncertainty notes",
        ),
        skill_ids=("synthesis.brief",),
        permissions=frozenset({SkillPermission.guild_member}),
    ),
    SpecialistRole(
        role_id=RoleId.web_researcher,
        title="Web Researcher",
        purpose="Retrieve a small set of external sources with citations when a provider is configured.",
        capabilities=("bounded web search", "citation extraction", "provider-unavailable reporting"),
        skill_ids=("research.web",),
        permissions=frozenset({SkillPermission.moderator}),
        implementation_note="Requires an explicitly configured Brave/Tavily provider; never fabricates sources.",
    ),
    SpecialistRole(
        role_id=RoleId.content_writer,
        title="Content Writer",
        purpose="Prepare a structured content brief grounded in approved community knowledge.",
        capabilities=("draft brief preparation", "voice-aware outline", "fact/provenance notes"),
        skill_ids=("content.brief",),
        permissions=frozenset({SkillPermission.moderator}),
        requires_human_approval=True,
        implementation_note=(
            "Produces a draft suggestion only; saving, versioning, approval and publishing "
            "remain human-gated in Content Studio."
        ),
    ),
    SpecialistRole(
        role_id=RoleId.community_analytics,
        title="Community Analytics / Problem Radar",
        purpose="Summarize aggregate community health, recurring errors and open questions.",
        capabilities=("community pulse", "aggregate insights", "problem radar", "recurring-problem evidence"),
        skill_ids=("pulse.community", "insights.community", "radar.community"),
        permissions=frozenset({SkillPermission.moderator}),
    ),
    SpecialistRole(
        role_id=RoleId.knowledge_memory,
        title="Knowledge and Memory",
        purpose="Use approved guild knowledge and list review candidates for moderators.",
        capabilities=("approved knowledge search", "candidate review listing", "provenance"),
        skill_ids=("kb.search", "knowledge.candidates"),
        permissions=frozenset({SkillPermission.moderator}),
    ),
    SpecialistRole(
        role_id=RoleId.ai_moderation,
        title="AI Moderation",
        purpose="Assess configured abuse categories with confidence and route cases to humans.",
        capabilities=("context classification", "incident reporting", "policy thresholds"),
        skill_ids=("moderation.assess",),
        permissions=frozenset({SkillPermission.moderator}),
        requires_human_approval=True,
        implementation_note=(
            "The host skill is classification-only. Deletion, warnings and incident review "
            "are handled by the dedicated moderation workflow; the host never acts."
        ),
    ),
    SpecialistRole(
        role_id=RoleId.vision_troubleshooting,
        title="Vision and Troubleshooting",
        purpose="Reason over screenshots, pasted errors and logs, separating observations from inferences.",
        capabilities=("screenshot evidence", "log/stack-trace analysis", "safe diagnostic steps"),
        skill_ids=("triage.analyze", "vision.analyze"),
        permissions=frozenset({SkillPermission.guild_member}),
        implementation_note=(
            "Text/log triage is lexical. Host screenshots use vision.analyze only after the "
            "configured provider reports vision support; otherwise the step reports an honest "
            "text-only fallback. PDF/DOCX uploads remain bounded text extraction in mention-chat."
        ),
    ),
    SpecialistRole(
        role_id=RoleId.quality_evaluation,
        title="Quality and Evaluation",
        purpose="Check plan bounds, provenance, guild isolation and safety invariants offline.",
        capabilities=("contract checks", "isolation checks", "approval invariants", "regression checks"),
        skill_ids=("quality.checks",),
        permissions=frozenset({SkillPermission.moderator}),
        implementation_note=(
            "The host skill runs the same offline checks as /evaluate without persisting a report; "
            "it never calls a model."
        ),
    ),
)


class RoleRegistry:
    """Lookup and validation for the fixed specialist roster."""

    def __init__(self, roles: tuple[SpecialistRole, ...] = ROLE_DEFINITIONS) -> None:
        seen: set[str] = set()
        for role in roles:
            if role.role_id.value in seen:
                raise ValueError(f"duplicate role id {role.role_id.value}")
            seen.add(role.role_id.value)
            if not role.capabilities:
                raise ValueError(f"{role.role_id.value} declares no capabilities")
            if role.writes_public_content and not role.requires_human_approval:
                raise ValueError(
                    f"{role.role_id.value} writes public content without human approval"
                )
        self._roles = {role.role_id: role for role in roles}

    def all(self) -> tuple[SpecialistRole, ...]:
        return tuple(self._roles.values())

    def get(self, role_id: RoleId | str) -> SpecialistRole:
        key = role_id if isinstance(role_id, RoleId) else RoleId(str(role_id))
        return self._roles[key]

    def role_for_skill(self, skill_id: str) -> SpecialistRole | None:
        for role in self._roles.values():
            if skill_id in role.skill_ids:
                return role
        return None

    def available(self, registered_skill_ids: frozenset[str] | set[str]) -> dict[RoleId, bool]:
        """Role availability based on at least one registered runtime skill."""
        return {
            role.role_id: any(
                skill_id in registered_skill_ids for skill_id in role.skill_ids
            )
            for role in self._roles.values()
        }
