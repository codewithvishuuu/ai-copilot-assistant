"""Phase 1 agent foundation: typed skills and a deterministic orchestrator.

The model does not route or execute anything here. Skills are application code
selected by explicit IDs; only read-only services are registered.
"""

from .builtin import build_default_registry
from .contract import (
    Skill,
    SkillContext,
    SkillErrorPolicy,
    SkillInput,
    SkillLimits,
    SkillOutput,
    SkillPermission,
    SkillResult,
    SkillStatus,
    TaskRequest,
)
from .orchestrator import SkillOrchestrator
from .registry import SkillRegistry

__all__ = [
    "Skill",
    "SkillContext",
    "SkillErrorPolicy",
    "SkillInput",
    "SkillLimits",
    "SkillOrchestrator",
    "SkillOutput",
    "SkillPermission",
    "SkillRegistry",
    "SkillResult",
    "SkillStatus",
    "TaskRequest",
    "build_default_registry",
]
