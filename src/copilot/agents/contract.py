"""Typed contract shared by every Phase 1 skill.

A skill is application code with a stable ID, a Pydantic input model, a
Pydantic output model, explicit permissions, hard limits, and a declared error
policy. The registry accepts only ``Skill`` subclasses -- never arbitrary
callables -- and Phase 1 accepts only read-only skills.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# Keep limits deliberately small: these are read-only local services, not a
# second inference system. Registry validation rejects values outside the caps.
SKILL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,63}$"
SKILL_ID_RE = re.compile(SKILL_ID_PATTERN)
MAX_SKILL_TIMEOUT_S = 30.0
MAX_SKILL_OUTPUT_CHARS = 20_000
MAX_REFERENCES = 20


class SkillPermission(str, Enum):
    """Application-level grants required before a skill may execute."""

    guild_member = "guild_member"
    moderator = "moderator"


class SkillErrorPolicy(str, Enum):
    """How the orchestrator treats failures.

    Phase 1 has one policy: return a structured, safe error and never leak the
    exception text to callers. A future phase may add explicit alternatives.
    """

    safe_error = "safe_error"


class SkillStatus(str, Enum):
    """Stable machine-readable outcome of one orchestrated request."""

    ok = "ok"
    denied = "denied"
    unknown_skill = "unknown_skill"
    invalid_input = "invalid_input"
    timeout = "timeout"
    provider_unavailable = "provider_unavailable"
    provider_error = "provider_error"
    invalid_output = "invalid_output"
    failed = "failed"


class Reference(BaseModel):
    """A bounded evidence pointer returned by a skill (never fetched by the bot)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=1000)
    snippet: str = Field(default="", max_length=400)


class SkillInput(BaseModel):
    """Base class for skill-specific input; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SkillOutput(BaseModel):
    """Base class for skill-specific output; references are bounded."""

    model_config = ConfigDict(extra="forbid")

    references: list[Reference] = Field(default_factory=list, max_length=MAX_REFERENCES)


@dataclass(frozen=True)
class SkillLimits:
    """Hard per-skill execution and serialization bounds."""

    timeout_s: float
    max_output_chars: int


@dataclass(frozen=True)
class SkillContext:
    """Trusted caller scope established before orchestration.

    ``permissions`` must be supplied by a trusted Discord-facing adapter (not
    built in Phase 1) from the existing moderator/dev checks; the model never
    sets or widens it.
    """

    guild_id: int
    channel_id: int
    parent_id: int | None
    user_id: int
    permissions: frozenset[SkillPermission]


class TaskRequest(BaseModel):
    """A structured request; only registered skill IDs are accepted by dispatch."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(min_length=2, max_length=64, pattern=SKILL_ID_PATTERN)
    input: dict[str, Any] = Field(default_factory=dict)


class SkillResult(BaseModel):
    """A structured orchestration result with safe errors and optional evidence."""

    model_config = ConfigDict(extra="forbid")

    status: SkillStatus
    skill_id: str | None = Field(default=None, max_length=64)
    output: dict[str, Any] | None = None
    references: list[Reference] = Field(default_factory=list, max_length=MAX_REFERENCES)
    error: str | None = Field(default=None, max_length=300)


InputT = TypeVar("InputT", bound=SkillInput)
OutputT = TypeVar("OutputT", bound=SkillOutput)


class Skill(ABC, Generic[InputT, OutputT]):
    """Typed application capability; register instances, never bare functions."""

    skill_id: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[SkillInput]]
    output_model: ClassVar[type[SkillOutput]]
    permissions: ClassVar[frozenset[SkillPermission]]
    limits: ClassVar[SkillLimits]
    read_only: ClassVar[bool] = True
    error_policy: ClassVar[SkillErrorPolicy] = SkillErrorPolicy.safe_error

    @abstractmethod
    async def execute(self, context: SkillContext, payload: InputT) -> OutputT:
        """Run the skill for one authorized caller scope."""
        raise NotImplementedError
