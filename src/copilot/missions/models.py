"""Typed mission models shared by the planner, runner and Discord views."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..agents.contract import Reference


class MissionMode(str, Enum):
    community = "community"
    research = "research"
    draft_prep = "draft_prep"


class MissionStatus(str, Enum):
    planned = "planned"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, le=20)
    title: str = Field(min_length=1, max_length=120)
    skill_id: str = Field(min_length=2, max_length=64)
    input: dict = Field(default_factory=dict)


class MissionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: MissionMode
    steps: list[PlannedStep] = Field(min_length=1, max_length=6)


class MissionStepView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int
    index: int
    title: str
    skill_id: str
    status: StepStatus
    error: str | None = None
    references: list[Reference] = Field(default_factory=list)
    output_summary: str = ""
    started_at: str | None = None
    finished_at: str | None = None


class MissionView(BaseModel):
    """One guild-scoped mission and its redacted, bounded progress state."""

    model_config = ConfigDict(extra="forbid")

    mission_id: int
    guild_id: str
    channel_id: str
    requester_id: str
    goal: str
    mode: MissionMode
    status: MissionStatus
    trace_id: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    cancel_requested: bool = False
    steps: list[MissionStepView] = Field(default_factory=list)
    summary: str = ""
    references: list[Reference] = Field(default_factory=list)

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def completed_steps(self) -> int:
        # Skipped/cancelled steps are terminal but did not produce a result;
        # counting them as progress would overstate what the mission achieved.
        return sum(1 for step in self.steps if step.status is StepStatus.succeeded)

    def progress_text(self) -> str:
        return f"{self.completed_steps}/{self.total_steps} step(s)"

    def can_cancel(self) -> bool:
        return self.status in (MissionStatus.planned, MissionStatus.running) and not self.cancel_requested

    def active_step(self) -> MissionStepView | None:
        for step in self.steps:
            if step.status is StepStatus.running:
                return step
        return None


class AuditActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    target_type: str | None = None
    target_id: str | None = None
    created_at: str


class MissionControlView(BaseModel):
    """Mission Control read model: missions, counts and recent guild activity."""

    model_config = ConfigDict(extra="forbid")

    guild_id: str
    counts: dict[str, int] = Field(default_factory=dict)
    active: list[MissionView] = Field(default_factory=list)
    recent: list[MissionView] = Field(default_factory=list)
    recent_activity: list[AuditActivity] = Field(default_factory=list)
    providers: dict[str, str] = Field(default_factory=dict)
