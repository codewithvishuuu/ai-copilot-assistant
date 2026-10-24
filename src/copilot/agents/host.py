"""Main Host Agent: bounded planning, specialist delegation and traceable execution.

The host is deterministic application code, not a model-controlled tool loop:

- it classifies the goal into a bounded mode, chooses specialist roles whose
  skills are actually registered, and builds a dependency graph;
- independent steps run concurrently under a hard semaphore; dependent steps
  wait for their upstreams and receive those outputs explicitly;
- every step goes through the existing read-only ``SkillOrchestrator``, so the
  registry, guild/channel allowlists, permissions, input validation and timeout
  caps are reused rather than duplicated;
- cancellation, partial failure, persistence, startup recovery and audit reuse
  the same durable patterns as missions.

No step can publish, delete, warn, execute user code, or call a model directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..agents.contract import (
    Skill,
    SkillContext,
    SkillPermission,
    SkillResult,
    SkillStatus,
    TaskRequest,
)
from ..agents.orchestrator import SkillOrchestrator
from ..agents.roles import RoleId, RoleRegistry
from ..db.repository import Repository
from ..errors import (
    AgentPlanError,
    AgentRunConflictError,
    AgentRunNotFoundError,
    NotModeratorError,
)
from ..util.text import strip_control_tokens, truncate

log = logging.getLogger(__name__)

MAX_GOAL_CHARS = 800
MAX_PLAN_STEPS = 6
MAX_RESULT_CHARS = 20_000
_UPSTREAM_KEY = "$upstream"
_TERMINAL = {"completed", "partial", "failed", "cancelled"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


class HostMode(str, Enum):
    community = "community"
    research = "research"
    draft_prep = "draft_prep"
    moderation = "moderation"
    triage = "triage"
    evaluation = "evaluation"


class AgentRunStatus(str, Enum):
    planned = "planned"
    running = "running"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class AgentStepStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class HostTaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    role_id: RoleId
    title: str = Field(min_length=3, max_length=120)
    skill_id: str = Field(min_length=2, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[int, ...] = ()


class HostPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    mode: HostMode
    tasks: tuple[HostTaskPlan, ...]
    unavailable: tuple[str, ...] = ()
    truncated: bool = False


class AgentStepView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    role_id: str
    title: str
    skill_id: str
    status: AgentStepStatus
    depends_on: tuple[int, ...]
    output: dict[str, Any] | None = None
    references: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class AgentRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int
    guild_id: str
    goal: str
    mode: str
    status: AgentRunStatus
    trace_id: str
    steps: list[AgentStepView] = Field(default_factory=list)
    summary: str = ""
    error_code: str | None = None
    cancel_requested: bool = False
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def completed_steps(self) -> int:
        return sum(1 for step in self.steps if step.status is AgentStepStatus.succeeded)

    def progress_text(self) -> str:
        return f"{self.completed_steps}/{len(self.steps)} step(s)"


@dataclass(frozen=True)
class AgentControlView:
    guild_id: str
    counts: dict[str, int]
    recent: list[AgentRunView]
    roles: list[dict[str, Any]]
    providers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------- planning

_KEYWORDS: tuple[tuple[HostMode, tuple[str, ...]], ...] = (
    (
        HostMode.moderation,
        ("moderat", "abuse", "harass", "threat", "spam", "toxic", "raid", "bully"),
    ),
    (
        HostMode.research,
        ("research", "source", " web ", "latest", "compare", "cite", "what is new"),
    ),
    (
        HostMode.draft_prep,
        ("draft", "announce", "write", "content", "faq", "newsletter", "event post"),
    ),
    (
        HostMode.triage,
        ("screenshot", "image", "traceback", "stack trace", "log", "error", "debug", "broken"),
    ),
    (
        HostMode.evaluation,
        ("evaluate", "evaluation", "quality check", "self-test", "regression", "offline checks"),
    ),
)


def classify_goal(goal: str) -> HostMode:
    text = f" {goal.casefold()} "
    for mode, needles in _KEYWORDS:
        if any(needle in text for needle in needles):
            return mode
    return HostMode.community


class HostPlanner:
    """Turn one goal into a bounded, dependency-aware plan over registered skills."""

    def __init__(self, settings: Any, *, roles: RoleRegistry | None = None) -> None:
        self._settings = settings
        self.roles = roles or RoleRegistry()

    def plan(
        self,
        goal: str,
        registered_skill_ids: set[str] | frozenset[str],
        *,
        mode: HostMode | None = None,
        has_images: bool = False,
    ) -> HostPlan:
        goal = truncate(strip_control_tokens(goal or "").strip(), MAX_GOAL_CHARS)
        limit = max(1, min(int(self._settings.agent_max_steps), MAX_PLAN_STEPS))
        if len(goal) < 3:
            raise AgentPlanError()
        chosen_mode = mode or classify_goal(goal)
        available = set(registered_skill_ids)
        sources: list[HostTaskPlan] = []
        unavailable: list[str] = []

        def add(
            role_id: RoleId,
            title: str,
            skill_id: str,
            payload: dict[str, Any] | None = None,
        ) -> bool:
            if skill_id not in available:
                return False
            sources.append(
                HostTaskPlan(
                    index=len(sources),
                    role_id=role_id,
                    title=title,
                    skill_id=skill_id,
                    input=dict(payload or {}),
                )
            )
            return True

        if chosen_mode is HostMode.community:
            add(RoleId.community_analytics, "Community pulse snapshot", "pulse.community")
            add(RoleId.community_analytics, "Aggregate community insights", "insights.community")
            if not add(
                RoleId.community_analytics, "Recurring-problem radar", "radar.community"
            ):
                unavailable.append(
                    "Problem Radar is unavailable in this deployment; the plan uses pulse, "
                    "insights and approved knowledge."
                )
            add(
                RoleId.knowledge_memory,
                "Search approved knowledge",
                "kb.search",
                {"query": goal[:200], "limit": 3},
            )
        elif chosen_mode is HostMode.research:
            add(
                RoleId.knowledge_memory,
                "Search approved knowledge",
                "kb.search",
                {"query": goal[:200], "limit": 3},
            )
            if not add(
                RoleId.web_researcher,
                "Search external sources",
                "research.web",
                {"query": goal[:500]},
            ):
                unavailable.append(
                    "Web Researcher is unavailable: no search provider is configured, "
                    "so this plan uses approved local knowledge only."
                )
        elif chosen_mode is HostMode.draft_prep:
            add(
                RoleId.knowledge_memory,
                "Search approved knowledge",
                "kb.search",
                {"query": goal[:200], "limit": 3},
            )
            add(RoleId.community_analytics, "Community pulse snapshot", "pulse.community")
            if not add(
                RoleId.content_writer,
                "Draft content suggestion",
                "content.brief",
                {
                    "topic": goal[:500],
                    "content_type": "announcement",
                    "length": "medium",
                },
            ):
                unavailable.append(
                    "Content Writer is unavailable because no generation provider is "
                    "configured; the plan cannot produce a draft suggestion."
                )
            if add(
                RoleId.web_researcher,
                "Optional source check",
                "research.web",
                {"query": goal[:500]},
            ):
                pass
            unavailable.append(
                "Content Writer produces a draft suggestion only; saving, versioning, "
                "approval and publishing remain human-gated in Content Studio."
            )
        elif chosen_mode is HostMode.moderation:
            if not add(
                RoleId.ai_moderation,
                "Assess reported text (read-only)",
                "moderation.assess",
                {"content": goal[:4000]},
            ):
                unavailable.append(
                    "The AI Moderation specialist is unavailable; the host will not guess "
                    "at a classification without the configured moderation service."
                )
            add(
                RoleId.knowledge_memory,
                "Review knowledge candidates",
                "knowledge.candidates",
                {"limit": 5},
            )
            add(RoleId.community_analytics, "Aggregate community insights", "insights.community")
            unavailable.append(
                "The host assessment is read-only and classification-only. Deletion, warnings, "
                "incidents and human review belong to the dedicated moderation workflow."
            )
        elif chosen_mode is HostMode.triage:
            add(
                RoleId.knowledge_memory,
                "Search approved knowledge",
                "kb.search",
                {"query": goal[:200], "limit": 3},
            )
            if has_images:
                if not add(
                    RoleId.vision_troubleshooting,
                    "Analyze screenshot evidence",
                    "vision.analyze",
                    {"goal": goal[:500]},
                ):
                    unavailable.append(
                        "Image analysis is unavailable because no vision-capable provider is "
                        "registered; this plan uses text/log triage only."
                    )
            if not add(
                RoleId.vision_troubleshooting,
                "Analyze error evidence",
                "triage.analyze",
                {"content": goal[:12000]},
            ):
                unavailable.append(
                    "Text/log triage is unavailable in this deployment."
                )
            if not has_images:
                unavailable.append(
                    "No screenshot was supplied; the host performs text/log triage only. "
                    "PDF/DOCX uploads remain bounded text extraction in mention-chat."
                )
        else:  # evaluation
            if not add(
                RoleId.quality_evaluation,
                "Run offline quality checks",
                "quality.checks",
            ):
                unavailable.append(
                    "Quality and Evaluation is unavailable until the offline evaluation "
                    "service is wired into this deployment."
                )

        if not sources:
            raise AgentPlanError()

        budget_for_sources = limit - 1 if limit >= 2 else 1
        truncated = len(sources) > budget_for_sources
        sources = sources[:budget_for_sources]
        tasks = list(sources)
        if limit >= 2 and "synthesis.brief" in available:
            combine_role = (
                RoleId.content_writer
                if chosen_mode is HostMode.draft_prep
                else RoleId.community_manager
            )
            tasks.append(
                HostTaskPlan(
                    index=len(tasks),
                    role_id=combine_role,
                    title="Combine specialist results",
                    skill_id="synthesis.brief",
                    input={"goal": goal, _UPSTREAM_KEY: True},
                    depends_on=tuple(task.index for task in sources),
                )
            )
        elif "synthesis.brief" not in available:
            unavailable.append(
                "Result combination is unavailable because synthesis.brief is not registered."
            )
        elif not truncated:
            unavailable.append(
                "Result combination was omitted to keep the plan within the 1-step budget."
            )
        if truncated:
            unavailable.append(
                f"The plan was capped at {limit} step(s); additional available checks were omitted."
            )
        return HostPlan(
            goal=goal,
            mode=chosen_mode,
            tasks=tuple(tasks),
            unavailable=tuple(unavailable),
            truncated=truncated,
        )


# --------------------------------------------------------------------- service


class AgentRunService:
    """Persist, execute, cancel and recover host-agent runs."""

    def __init__(
        self,
        repo: Repository,
        orchestrator: SkillOrchestrator,
        settings: Any,
        *,
        planner: HostPlanner | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._orchestrator = orchestrator
        self._settings = settings
        self._planner = planner or HostPlanner(settings)
        self._clock = clock
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancel_events: dict[int, asyncio.Event] = {}
        # Image bytes stay in this in-memory map only; they are never written to
        # plan_json, input_json, step output or the database.
        self._images: dict[int, tuple[bytes, ...]] = {}

    # ---------------------------------------------------------------- creation

    async def create_run(
        self,
        context: SkillContext,
        *,
        goal: str,
        mode: HostMode | None = None,
        has_images: bool = False,
        idempotency_key: str | None = None,
    ) -> AgentRunView:
        if SkillPermission.moderator not in context.permissions:
            raise NotModeratorError()
        if not self._settings.agent_enabled:
            raise AgentRunConflictError("Agent runs are disabled on this bot.")
        clean_goal = truncate(
            strip_control_tokens(goal or "").strip(),
            min(MAX_GOAL_CHARS, int(self._settings.agent_max_goal_chars)),
        )
        if len(clean_goal) < 3:
            raise AgentPlanError()
        if idempotency_key:
            existing = await self._repo.find_agent_run_by_idempotency(
                guild_id=str(context.guild_id), idempotency_key=idempotency_key
            )
            if existing is not None:
                return await self.get(guild_id=str(context.guild_id), run_id=int(existing["id"]))
        plan = self._planner.plan(
            clean_goal,
            self._orchestrator.registry.ids,
            mode=mode,
            has_images=has_images,
        )
        self._validate_plan(plan)
        now = _iso(self._clock())
        trace_id = f"agent-{context.guild_id}-{now}"
        steps = [
            (
                task.index,
                task.role_id.value,
                task.title,
                task.skill_id,
                json.dumps(list(task.depends_on)),
                json.dumps(task.input),
            )
            for task in plan.tasks
        ]
        run_id = await self._repo.insert_agent_run_with_steps(
            guild_id=str(context.guild_id),
            channel_id=str(context.channel_id),
            parent_channel_id=None if context.parent_id is None else str(context.parent_id),
            requester_id=str(context.user_id),
            goal=clean_goal,
            mode=plan.mode.value,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            created_at=now,
            plan_json=plan.model_dump_json(),
            steps=steps,
            audit_meta={
                "guild_id": str(context.guild_id),
                "mode": plan.mode.value,
                "steps": len(plan.tasks),
                "unavailable": len(plan.unavailable),
                "image_inputs": has_images,
            },
        )
        return await self.get(guild_id=str(context.guild_id), run_id=run_id)

    # ------------------------------------------------------------------ run

    async def start(
        self,
        context: SkillContext,
        run_id: int,
        *,
        images: Sequence[bytes] = (),
        on_update: Callable[[AgentRunView], Awaitable[None]] | None = None,
    ) -> AgentRunView:
        if SkillPermission.moderator not in context.permissions:
            raise NotModeratorError()
        row = await self._repo.get_agent_run(run_id, guild_id=str(context.guild_id))
        if row is None:
            raise AgentRunNotFoundError()
        if row["status"] != AgentRunStatus.planned.value:
            raise AgentRunConflictError()
        started_at = _iso(self._clock())
        if not await self._repo.start_agent_run(
            run_id, guild_id=str(context.guild_id), started_at=started_at
        ):
            raise AgentRunConflictError()
        await self._repo.audit(
            actor_id=str(context.user_id),
            action="agent.started",
            target_type="agent_run",
            target_id=str(run_id),
            guild_id=str(context.guild_id),
            meta={"guild_id": str(context.guild_id), "mode": str(row["mode"])},
        )
        cancel_event = asyncio.Event()
        self._cancel_events[run_id] = cancel_event
        if images:
            self._images[run_id] = tuple(images)
        try:
            task = asyncio.create_task(
                self._run(context, run_id, cancel_event, on_update),
                name=f"copilot-agent-run-{run_id}",
            )
        except Exception:
            self._images.pop(run_id, None)
            raise
        self._tasks[run_id] = task
        return await self.get(guild_id=str(context.guild_id), run_id=run_id)

    async def _run(
        self,
        context: SkillContext,
        run_id: int,
        cancel_event: asyncio.Event,
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
    ) -> None:
        try:
            await self._execute(context, run_id, cancel_event, on_update)
        except asyncio.CancelledError:
            await self._cancel_now(context, run_id, on_update)
            raise
        except Exception:  # noqa: BLE001 - a run failure must be persisted honestly
            log.exception("Agent run %s crashed", run_id)
            await self._fail_now(context, run_id, "internal_error", on_update)
        finally:
            self._tasks.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._images.pop(run_id, None)

    async def _execute(
        self,
        context: SkillContext,
        run_id: int,
        cancel_event: asyncio.Event,
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
    ) -> None:
        rows = await self._repo.list_agent_run_steps(run_id, guild_id=str(context.guild_id))
        if not rows:
            await self._fail_now(context, run_id, "empty_plan", on_update)
            return
        statuses: dict[int, AgentStepStatus] = {}
        outputs: dict[int, dict[str, Any]] = {}
        events: dict[int, asyncio.Event] = {}
        for row in rows:
            index = int(row["step_index"])
            statuses[index] = AgentStepStatus.pending
            events[index] = asyncio.Event()
        semaphore = asyncio.Semaphore(max(1, int(self._settings.agent_max_parallel)))

        async def run_step(row: Any) -> None:
            index = int(row["step_index"])
            depends = _json_list(row["depends_on_json"])
            try:
                for dependency in depends:
                    if dependency not in events:
                        await self._finish_step(
                            context, run_id, row, AgentStepStatus.skipped,
                            "dependency is missing from the plan",
                            statuses, events, on_update,
                        )
                        return
                    await events[dependency].wait()
                    if statuses[dependency] is not AgentStepStatus.succeeded:
                        await self._finish_step(
                            context, run_id, row, AgentStepStatus.skipped,
                            "an upstream step did not succeed",
                            statuses, events, on_update,
                        )
                        return
                if cancel_event.is_set():
                    await self._finish_step(
                        context, run_id, row, AgentStepStatus.cancelled, None,
                        statuses, events, on_update,
                    )
                    return
                async with semaphore:
                    if cancel_event.is_set():
                        await self._finish_step(
                            context, run_id, row, AgentStepStatus.cancelled, None,
                            statuses, events, on_update,
                        )
                        return
                    started = _iso(self._clock())
                    if not await self._repo.start_agent_run_step(
                        int(row["id"]), run_id=run_id, started_at=started
                    ):
                        await self._finish_step(
                            context, run_id, row, AgentStepStatus.cancelled, None,
                            statuses, events, on_update,
                        )
                        return
                    await self._notify(context, run_id, on_update)
                    payload = _json_dict(row["input_json"])
                    if payload.pop(_UPSTREAM_KEY, False):
                        payload["upstream"] = {
                            f"step {dependency}": outputs.get(dependency, {})
                            for dependency in depends
                        }
                    if str(row["skill_id"]) == "vision.analyze":
                        payload["images"] = list(self._images.get(run_id, ()))
                    result = await self._orchestrator.run(
                        TaskRequest(skill_id=str(row["skill_id"]), input=payload), context
                    )
                    if result.status is SkillStatus.ok and result.output is not None:
                        outputs[index] = result.output
                        await self._finish_step(
                            context, run_id, row, AgentStepStatus.succeeded, None,
                            statuses, events, on_update, result=result,
                        )
                    else:
                        await self._finish_step(
                            context, run_id, row, AgentStepStatus.failed,
                            result.error or f"skill returned {result.status.value}",
                            statuses, events, on_update,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - isolate one broken step
                log.exception("Agent step %s failed", index)
                await self._finish_step(
                    context, run_id, row, AgentStepStatus.failed,
                    "step failed safely", statuses, events, on_update,
                )
            finally:
                events[index].set()

        workers = [asyncio.create_task(run_step(row)) for row in rows]
        try:
            _done, pending = await asyncio.wait(
                workers, timeout=float(self._settings.agent_run_timeout_s)
            )
        except asyncio.CancelledError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        timed_out = bool(pending)
        if timed_out:
            for worker in pending:
                worker.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        if timed_out:
            await self._repo.close_open_agent_steps(
                run_id,
                status="cancelled",
                finished_at=_iso(self._clock()),
                error="run timed out",
            )
            await self._finalize(context, run_id, "timeout", on_update)
            return
        if cancel_event.is_set():
            await self._finalize(context, run_id, "cancelled", on_update)
            return
        await self._finalize(context, run_id, None, on_update)

    async def _finish_step(
        self,
        context: SkillContext,
        run_id: int,
        row: Any,
        status: AgentStepStatus,
        error: str | None,
        statuses: dict[int, AgentStepStatus],
        events: dict[int, asyncio.Event],
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
        *,
        result: SkillResult | None = None,
    ) -> None:
        index = int(row["step_index"])
        if statuses[index] is not AgentStepStatus.pending:
            return
        statuses[index] = status
        finished_at = _iso(self._clock())
        output_json = (
            json.dumps(result.output, ensure_ascii=False, default=str)
            if result is not None and result.output
            else None
        )
        references_json = json.dumps(
            [reference.model_dump() for reference in (result.references if result else [])],
            ensure_ascii=False,
        )
        await self._repo.finish_agent_run_step(
            int(row["id"]),
            run_id=run_id,
            status=status.value,
            output_json=output_json,
            references_json=references_json,
            error=truncate(error or "", 300) if error else None,
            finished_at=finished_at,
        )
        events[index].set()
        await self._repo.audit(
            actor_id=str(context.user_id),
            action="agent.step_finished",
            target_type="agent_run",
            target_id=str(run_id),
            guild_id=str(context.guild_id),
            meta={
                "guild_id": str(context.guild_id),
                "step": index,
                "role": str(row["role_id"]),
                "skill": str(row["skill_id"]),
                "status": status.value,
            },
        )
        await self._notify(context, run_id, on_update)

    async def _finalize(
        self,
        context: SkillContext,
        run_id: int,
        forced: str | None,
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
    ) -> None:
        row = await self._repo.get_agent_run(run_id, guild_id=str(context.guild_id))
        if row is None:
            return
        steps = await self._repo.list_agent_run_steps(run_id, guild_id=str(context.guild_id))
        statuses = [AgentStepStatus(str(step["status"])) for step in steps]
        succeeded = sum(1 for status in statuses if status is AgentStepStatus.succeeded)
        failed = sum(1 for status in statuses if status is AgentStepStatus.failed)
        skipped = sum(1 for status in statuses if status is AgentStepStatus.skipped)
        cancel_requested = bool(row["cancel_requested_at"]) or forced == "cancelled"
        if cancel_requested:
            terminal = AgentRunStatus.cancelled
            error_code = "cancelled"
        elif forced == "timeout":
            terminal = AgentRunStatus.partial if succeeded else AgentRunStatus.failed
            error_code = "timeout"
        elif failed or skipped:
            terminal = AgentRunStatus.partial if succeeded else AgentRunStatus.failed
            error_code = "partial_failure" if succeeded else "failed"
        else:
            terminal = AgentRunStatus.completed
            error_code = None
        open_status = "cancelled"
        await self._repo.close_open_agent_steps(
            run_id, status=open_status, finished_at=_iso(self._clock()), error=error_code
        )
        if terminal in (AgentRunStatus.failed, AgentRunStatus.cancelled):
            await self._repo.close_open_agent_steps(
                run_id, status=open_status, finished_at=_iso(self._clock()), error=error_code
            )
        summary, references, result_json = _build_result(row, steps, terminal, error_code)
        finished_at = _iso(self._clock())
        await self._repo.finish_agent_run(
            run_id,
            guild_id=str(context.guild_id),
            status=terminal.value,
            result_json=result_json,
            error_code=error_code,
            finished_at=finished_at,
        )
        await self._repo.audit(
            actor_id=str(context.user_id),
            action=f"agent.{terminal.value}",
            target_type="agent_run",
            target_id=str(run_id),
            guild_id=str(context.guild_id),
            meta={
                "guild_id": str(context.guild_id),
                "steps": len(steps),
                "succeeded": succeeded,
                "failed": failed,
                "skipped": skipped,
                "error_code": error_code,
            },
        )
        await self._notify(context, run_id, on_update)

    async def _notify(
        self,
        context: SkillContext,
        run_id: int,
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
    ) -> None:
        if on_update is None:
            return
        try:
            view = await self.get(guild_id=str(context.guild_id), run_id=run_id)
            await on_update(view)
        except Exception:  # noqa: BLE001 - progress delivery must not break the run
            log.warning("Agent run %s progress callback failed", run_id)

    async def _cancel_now(
        self,
        context: SkillContext,
        run_id: int,
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
    ) -> None:
        finished_at = _iso(self._clock())
        await self._repo.close_open_agent_steps(
            run_id, status="cancelled", finished_at=finished_at, error="cancelled"
        )
        row = await self._repo.get_agent_run(run_id, guild_id=str(context.guild_id))
        if row is None:
            return
        steps = await self._repo.list_agent_run_steps(run_id, guild_id=str(context.guild_id))
        summary, _references, result_json = _build_result(
            row, steps, AgentRunStatus.cancelled, "cancelled"
        )
        await self._repo.finish_agent_run(
            run_id,
            guild_id=str(context.guild_id),
            status=AgentRunStatus.cancelled.value,
            result_json=result_json,
            error_code="cancelled",
            finished_at=finished_at,
        )
        await self._repo.audit(
            actor_id=str(context.user_id),
            action="agent.cancelled",
            target_type="agent_run",
            target_id=str(run_id),
            guild_id=str(context.guild_id),
            meta={"guild_id": str(context.guild_id)},
        )
        await self._notify(context, run_id, on_update)

    async def _fail_now(
        self,
        context: SkillContext,
        run_id: int,
        error_code: str,
        on_update: Callable[[AgentRunView], Awaitable[None]] | None,
    ) -> None:
        finished_at = _iso(self._clock())
        await self._repo.close_open_agent_steps(
            run_id, status="cancelled", finished_at=finished_at, error=error_code
        )
        row = await self._repo.get_agent_run(run_id, guild_id=str(context.guild_id))
        if row is None:
            return
        steps = await self._repo.list_agent_run_steps(run_id, guild_id=str(context.guild_id))
        summary, _references, result_json = _build_result(
            row, steps, AgentRunStatus.failed, error_code
        )
        await self._repo.finish_agent_run(
            run_id,
            guild_id=str(context.guild_id),
            status=AgentRunStatus.failed.value,
            result_json=result_json,
            error_code=error_code,
            finished_at=finished_at,
        )
        await self._repo.audit(
            actor_id=str(context.user_id),
            action="agent.failed",
            target_type="agent_run",
            target_id=str(run_id),
            guild_id=str(context.guild_id),
            meta={"guild_id": str(context.guild_id), "error_code": error_code},
        )
        await self._notify(context, run_id, on_update)

    # ---------------------------------------------------------------- control

    async def cancel(self, context: SkillContext, run_id: int) -> AgentRunView:
        if SkillPermission.moderator not in context.permissions:
            raise NotModeratorError()
        row = await self._repo.get_agent_run(run_id, guild_id=str(context.guild_id))
        if row is None:
            raise AgentRunNotFoundError()
        if row["status"] in _TERMINAL:
            return await self.get(guild_id=str(context.guild_id), run_id=run_id)
        requested_at = _iso(self._clock())
        if await self._repo.request_agent_run_cancel(
            run_id, guild_id=str(context.guild_id), requested_at=requested_at
        ):
            await self._repo.audit(
                actor_id=str(context.user_id),
                action="agent.cancel_requested",
                target_type="agent_run",
                target_id=str(run_id),
                guild_id=str(context.guild_id),
                meta={"guild_id": str(context.guild_id)},
            )
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
        return await self.get(guild_id=str(context.guild_id), run_id=run_id)

    async def get(self, *, guild_id: str, run_id: int) -> AgentRunView:
        row = await self._repo.get_agent_run(run_id, guild_id=str(guild_id))
        if row is None:
            raise AgentRunNotFoundError()
        steps = await self._repo.list_agent_run_steps(run_id, guild_id=str(guild_id))
        return self._view(row, steps)

    async def list(self, *, guild_id: str, limit: int = 10) -> list[AgentRunView]:
        rows = await self._repo.list_agent_runs(guild_id=str(guild_id), limit=limit)
        views: list[AgentRunView] = []
        for row in rows:
            steps = await self._repo.list_agent_run_steps(int(row["id"]), guild_id=str(guild_id))
            views.append(self._view(row, steps))
        return views

    async def control_view(
        self, *, guild_id: str, providers: dict[str, str] | None = None
    ) -> AgentControlView:
        counts = await self._repo.agent_run_counts(guild_id=str(guild_id))
        recent = await self.list(guild_id=str(guild_id), limit=5)
        availability = self._planner.roles.available(set(self._orchestrator.registry.ids))
        roles = [
            {
                "role_id": role.role_id.value,
                "title": role.title,
                "skill_ids": list(role.skill_ids),
                "available": availability.get(role.role_id, False),
                "requires_human_approval": role.requires_human_approval,
                "note": role.implementation_note,
            }
            for role in self._planner.roles.all()
        ]
        return AgentControlView(
            guild_id=str(guild_id),
            counts=counts,
            recent=recent,
            roles=roles,
            providers=dict(providers or {}),
        )

    async def recover_interrupted(self) -> int:
        return await self._repo.fail_interrupted_agent_runs(finished_at=_iso(self._clock()))

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()

    # -------------------------------------------------------------- internals

    def _validate_plan(self, plan: HostPlan) -> None:
        if not plan.tasks or len(plan.tasks) > MAX_PLAN_STEPS:
            raise AgentPlanError()
        seen: set[int] = set()
        for task in plan.tasks:
            if task.index in seen:
                raise AgentPlanError()
            seen.add(task.index)
            if any(dependency not in seen or dependency >= task.index for dependency in task.depends_on):
                raise AgentPlanError()
            skill = self._orchestrator.registry.get(task.skill_id)
            if not isinstance(skill, Skill) or not skill.read_only:
                raise AgentPlanError()

    def _view(self, row: Any, steps: list[Any]) -> AgentRunView:
        result = _json_dict(row["result_json"]) if row["result_json"] else {}
        views = [
            AgentStepView(
                index=int(step["step_index"]),
                role_id=str(step["role_id"]),
                title=str(step["title"]),
                skill_id=str(step["skill_id"]),
                status=AgentStepStatus(str(step["status"])),
                depends_on=tuple(_json_list(step["depends_on_json"])),
                output=_json_dict(step["output_json"]) if step["output_json"] else None,
                references=_json_list(step["references_json"]),
                error=str(step["error"]) if step["error"] else None,
                started_at=str(step["started_at"]) if step["started_at"] else None,
                finished_at=str(step["finished_at"]) if step["finished_at"] else None,
            )
            for step in steps
        ]
        return AgentRunView(
            run_id=int(row["id"]),
            guild_id=str(row["guild_id"]),
            goal=str(row["goal"]),
            mode=str(row["mode"]),
            status=AgentRunStatus(str(row["status"])),
            trace_id=str(row["trace_id"]),
            steps=views,
            summary=str(result.get("summary") or ""),
            error_code=str(row["error_code"]) if row["error_code"] else None,
            cancel_requested=bool(row["cancel_requested_at"]),
            created_at=str(row["created_at"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
        )


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _build_result(
    row: Any, steps: list[Any], status: AgentRunStatus, error_code: str | None
) -> tuple[str, list[dict[str, Any]], str]:
    lines = [
        f"Agent run #{int(row['id'])} — {status.value}",
        f"Goal: {truncate(str(row['goal']), 300)}",
        f"Mode: {row['mode']}",
        f"Trace: {row['trace_id']}",
        "",
        "Steps:",
    ]
    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for step in steps:
        icon = {
            "succeeded": "OK",
            "failed": "FAILED",
            "skipped": "SKIPPED",
            "cancelled": "CANCELLED",
            "pending": "PENDING",
            "running": "RUNNING",
        }.get(str(step["status"]), str(step["status"]))
        lines.append(
            f"- [{step['role_id']}] {step['title']} ({step['skill_id']}): {icon}"
        )
        if step["error"]:
            lines.append(f"  error: {truncate(str(step['error']), 200)}")
        for reference in _json_list(step["references_json"]):
            if not isinstance(reference, dict):
                continue
            key = f"{reference.get('kind')}|{reference.get('title')}|{reference.get('url')}"
            if key in seen:
                continue
            seen.add(key)
            references.append(reference)
    lines.append("")
    if error_code:
        lines.append(f"Failure code: {error_code}")
    lines.append(
        "This trace lists read-only tool outcomes. It is not a verified real-world result."
    )
    summary = truncate("\n".join(lines), min(MAX_RESULT_CHARS, 8_000))
    result_json = json.dumps(
        {
            "summary": summary,
            "status": status.value,
            "error_code": error_code,
            "references": references[:20],
            "steps": [
                {
                    "index": int(step["step_index"]),
                    "role_id": str(step["role_id"]),
                    "skill_id": str(step["skill_id"]),
                    "status": str(step["status"]),
                }
                for step in steps
            ],
        },
        ensure_ascii=False,
    )
    return summary, references, result_json
