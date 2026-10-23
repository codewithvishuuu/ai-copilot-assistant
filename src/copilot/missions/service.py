"""Durable multi-step missions over typed, read-only skills.

A mission is created from a deterministic plan, executed one step at a time in
a background task, and persisted after every transition. Progress is exposed as
a guild-scoped read model; cancellation is cooperative between steps and also
cancels the in-flight skill. Missions never write community data, never publish,
and never call a tool the registry did not explicitly provide.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ..agents.contract import (
    Reference,
    SkillContext,
    SkillPermission,
    SkillStatus,
    TaskRequest,
)
from ..agents.orchestrator import SkillOrchestrator
from ..db.repository import Repository
from ..errors import (
    MissionConflictError,
    MissionNotFoundError,
    MissionPlanError,
    NotModeratorError,
)
from ..util.text import neutralize_mentions, strip_control_tokens, truncate
from .models import (
    AuditActivity,
    MissionControlView,
    MissionMode,
    MissionStatus,
    MissionStepView,
    MissionView,
    StepStatus,
)
from .planner import build_mission_plan

log = logging.getLogger(__name__)

MissionCallback = Callable[[MissionView], Awaitable[None]]
_TERMINAL = {MissionStatus.completed, MissionStatus.failed, MissionStatus.cancelled}
_MAX_SUMMARY_CHARS = 2000
_MAX_REFERENCES = 20


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _loads_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _loads_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _references_from(raw: str | None) -> list[Reference]:
    out: list[Reference] = []
    for item in _loads_list(raw)[:_MAX_REFERENCES]:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Reference.model_validate(item))
        except ValueError:
            continue
    return out


def _summarize_output(skill_id: str, raw: str | None) -> str:
    data = _loads_object(raw)
    if skill_id == "pulse.community":
        pulse = data.get("pulse") if isinstance(data.get("pulse"), dict) else {}
        total = pulse.get("total_threads")
        if pulse.get("sufficient") is False:
            return f"Not enough data yet: {total} summarized thread(s)."
        observations = pulse.get("observations") if isinstance(pulse.get("observations"), list) else []
        recommendations = (
            pulse.get("recommendations") if isinstance(pulse.get("recommendations"), list) else []
        )
        titles = [
            str(item.get("title"))
            for item in recommendations[:2]
            if isinstance(item, dict) and item.get("title")
        ]
        line = f"{total} thread(s); {len(observations)} observation(s)."
        if titles:
            line += " Top recommendations: " + "; ".join(titles) + "."
        return truncate(line, 500)
    if skill_id == "insights.community":
        total = data.get("total_threads")
        if data.get("sufficient") is False:
            return f"Not enough data yet: {total} summarized thread(s)."
        errors = data.get("top_errors") if isinstance(data.get("top_errors"), list) else []
        labels = [
            str(item[0])
            for item in errors[:3]
            if isinstance(item, (list, tuple)) and item and item[0]
        ]
        line = f"{total} thread(s) summarized."
        if labels:
            line += " Recurring errors: " + "; ".join(labels) + "."
        return truncate(line, 500)
    if skill_id == "kb.search":
        refs = data.get("references") if isinstance(data.get("references"), list) else []
        return f"{len(refs)} approved knowledge document(s) matched."
    if skill_id == "research.web":
        citations = data.get("citations") if isinstance(data.get("citations"), list) else []
        provider = str(data.get("provider") or "unknown")
        summary = str(data.get("summary") or "")
        return truncate(
            f"{provider}: {len(citations)} source(s). {summary}", 600
        ) if summary else f"{provider}: {len(citations)} source(s)."
    return ""


class MissionService:
    """Create, run, observe and cancel guild-scoped missions."""

    def __init__(
        self,
        repo: Repository,
        orchestrator: SkillOrchestrator,
        settings: Any,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._orchestrator = orchestrator
        self._settings = settings
        self._clock = clock
        self._tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------- authorization

    @staticmethod
    def _require_moderator(context: SkillContext) -> None:
        if SkillPermission.moderator not in context.permissions:
            raise NotModeratorError()

    @staticmethod
    def _now_iso() -> str:
        return _iso(_utc_now())

    # ------------------------------------------------------------------- create

    async def create_mission(
        self,
        context: SkillContext,
        *,
        goal: str,
        mode: MissionMode,
        idempotency_key: str | None = None,
    ) -> MissionView:
        self._require_moderator(context)
        if not getattr(self._settings, "missions_enabled", True):
            raise MissionConflictError("Missions are disabled on this bot.")
        clean_goal = " ".join(strip_control_tokens(goal or "").split())
        max_goal = int(getattr(self._settings, "mission_max_goal_chars", 500))
        if len(clean_goal) < 3:
            raise MissionPlanError("Describe the mission goal in at least three characters.")
        if len(clean_goal) > max_goal:
            raise MissionPlanError(
                f"The mission goal is too long ({len(clean_goal)} characters); keep it under {max_goal}."
            )

        if idempotency_key:
            existing = await self._repo.find_mission_by_idempotency(
                guild_id=str(context.guild_id), idempotency_key=idempotency_key
            )
            if existing is not None:
                return await self.get(guild_id=str(context.guild_id), mission_id=int(existing["id"]))

        plan = build_mission_plan(
            goal=clean_goal,
            mode=mode,
            settings=self._settings,
            available_skills=set(self._orchestrator.registry.ids),
        )
        now = self._now_iso()
        trace_id = secrets.token_hex(8)
        mission_id = await self._repo.insert_mission_with_steps(
            guild_id=str(context.guild_id),
            channel_id=str(context.channel_id),
            parent_channel_id=(
                None if context.parent_id is None else str(context.parent_id)
            ),
            requester_id=str(context.user_id),
            goal=clean_goal,
            mode=mode.value,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            created_at=now,
            plan_json=plan.model_dump_json(),
            steps=[
                (step.index, step.title, step.skill_id, json.dumps(step.input))
                for step in plan.steps
            ],
        )
        await self._audit(
            context.guild_id,
            context.user_id,
            mission_id,
            "mission.created",
            {"mode": mode.value, "steps": len(plan.steps), "trace_id": trace_id},
        )
        return await self.get(guild_id=str(context.guild_id), mission_id=mission_id)

    # -------------------------------------------------------------------- run

    async def start(
        self,
        context: SkillContext,
        mission_id: int,
        *,
        on_update: MissionCallback | None = None,
    ) -> MissionView:
        """Start (or resume) one mission in the background and return its state."""
        self._require_moderator(context)
        view = await self.get(guild_id=str(context.guild_id), mission_id=mission_id)
        if view.status in _TERMINAL:
            raise MissionConflictError("This mission has already finished.")
        existing = self._tasks.get(mission_id)
        if existing is not None and not existing.done():
            return view
        if view.cancel_requested:
            await self._finalize_cancel(str(context.guild_id), mission_id, str(context.user_id))
            return await self.get(guild_id=str(context.guild_id), mission_id=mission_id)

        task = asyncio.create_task(
            self._run(mission_id, context, on_update), name=f"mission-{mission_id}"
        )
        self._tasks[mission_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(mission_id, None))
        return view

    async def _run(
        self,
        mission_id: int,
        context: SkillContext,
        on_update: MissionCallback | None,
    ) -> None:
        guild_id = str(context.guild_id)
        started_at = self._now_iso()
        try:
            if not await self._repo.start_mission(
                mission_id, guild_id=guild_id, started_at=started_at
            ):
                # A cancel request may have won the race before the runner began.
                current = await self.get(guild_id=guild_id, mission_id=mission_id)
                if current.cancel_requested and current.status not in _TERMINAL:
                    await self._finalize_cancel(guild_id, mission_id, str(context.user_id))
                    await self._notify(guild_id, mission_id, on_update)
                return
            await self._audit(
                context.guild_id,
                context.user_id,
                mission_id,
                "mission.started",
                {"trace_id": (await self.get(guild_id=guild_id, mission_id=mission_id)).trace_id},
            )
            await self._notify(guild_id, mission_id, on_update)

            steps = await self._repo.list_mission_steps(mission_id, guild_id=guild_id)
            for row in steps:
                if row["status"] != StepStatus.pending.value:
                    continue
                if await self._repo.mission_cancel_requested(mission_id, guild_id=guild_id):
                    await self._finalize_cancel(guild_id, mission_id, str(context.user_id))
                    await self._notify(guild_id, mission_id, on_update)
                    return

                step_id = int(row["id"])
                step_started = self._now_iso()
                if not await self._repo.start_mission_step(
                    step_id, mission_id=mission_id, started_at=step_started
                ):
                    continue
                await self._notify(guild_id, mission_id, on_update)

                skill_id = str(row["skill_id"])
                payload = _loads_object(str(row["input_json"] or "{}"))
                result = await self._orchestrator.run(
                    TaskRequest(skill_id=skill_id, input=payload), context
                )
                finished = self._now_iso()
                if result.status is not SkillStatus.ok:
                    await self._repo.finish_mission_step(
                        step_id,
                        mission_id=mission_id,
                        status=StepStatus.failed.value,
                        output_json=None,
                        references_json="[]",
                        error=(result.error or "The step failed safely.")[:300],
                        finished_at=finished,
                    )
                    await self._finalize_failure(
                        guild_id,
                        mission_id,
                        str(context.user_id),
                        error_code=result.status.value,
                        step_error=result.error or "A mission step failed.",
                    )
                    await self._notify(guild_id, mission_id, on_update)
                    return

                output = result.output if isinstance(result.output, dict) else {}
                output_json = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
                max_chars = int(getattr(self._settings, "mission_max_result_chars", 8000))
                if len(output_json) > max_chars:
                    output_json = json.dumps(
                        {"truncated": True, "preview": output_json[:max_chars]}
                    )
                await self._repo.finish_mission_step(
                    step_id,
                    mission_id=mission_id,
                    status=StepStatus.succeeded.value,
                    output_json=output_json,
                    references_json=json.dumps(
                        [reference.model_dump(mode="json") for reference in result.references]
                    ),
                    error=None,
                    finished_at=finished,
                )
                await self._audit(
                    context.guild_id,
                    context.user_id,
                    mission_id,
                    "mission.step_finished",
                    {
                        "step": int(row["step_index"]),
                        "skill_id": skill_id,
                        "status": result.status.value,
                    },
                )
                await self._notify(guild_id, mission_id, on_update)

            await self._finalize_completed(guild_id, mission_id, str(context.user_id))
            await self._notify(guild_id, mission_id, on_update)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._finalize_cancel(guild_id, mission_id, str(context.user_id))
                await self._notify(guild_id, mission_id, on_update)
            raise
        except Exception as exc:  # noqa: BLE001 - runner failures must not crash the bot
            log.error(
                "Mission runner failed (mission=%s type=%s)", mission_id, type(exc).__name__
            )
            with contextlib.suppress(Exception):
                await self._finalize_failure(
                    guild_id,
                    mission_id,
                    str(context.user_id),
                    error_code="runner_error",
                    step_error="The mission runner failed safely.",
                )
                await self._notify(guild_id, mission_id, on_update)
        finally:
            self._tasks.pop(mission_id, None)

    # ------------------------------------------------------------------ cancel

    async def cancel(self, context: SkillContext, mission_id: int) -> MissionView:
        self._require_moderator(context)
        guild_id = str(context.guild_id)
        view = await self.get(guild_id=guild_id, mission_id=mission_id)
        if view.status in _TERMINAL:
            return view
        await self._repo.request_mission_cancel(
            mission_id, guild_id=guild_id, requested_at=self._now_iso()
        )
        task = self._tasks.get(mission_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait({task}, timeout=5.0)
        current = await self.get(guild_id=guild_id, mission_id=mission_id)
        if current.status not in _TERMINAL:
            await self._finalize_cancel(guild_id, mission_id, str(context.user_id))
            current = await self.get(guild_id=guild_id, mission_id=mission_id)
        return current

    # ------------------------------------------------------------------ reads

    async def get(self, *, guild_id: str, mission_id: int) -> MissionView:
        row = await self._repo.get_mission(int(mission_id), guild_id=str(guild_id))
        if row is None:
            raise MissionNotFoundError()
        steps = await self._repo.list_mission_steps(int(mission_id), guild_id=str(guild_id))
        return self._view(row, steps)

    async def list(self, *, guild_id: str, limit: int = 10) -> list[MissionView]:
        rows = await self._repo.list_missions(guild_id=str(guild_id), limit=limit)
        views: list[MissionView] = []
        for row in rows:
            steps = await self._repo.list_mission_steps(int(row["id"]), guild_id=str(guild_id))
            views.append(self._view(row, steps))
        return views

    async def control_view(
        self, *, guild_id: str, providers: dict[str, str] | None = None
    ) -> MissionControlView:
        rows = await self._repo.list_missions(guild_id=str(guild_id), limit=15)
        views: list[MissionView] = []
        for row in rows:
            steps = await self._repo.list_mission_steps(int(row["id"]), guild_id=str(guild_id))
            views.append(self._view(row, steps))
        active = [view for view in views if view.status not in _TERMINAL][:5]
        recent = [view for view in views if view.status in _TERMINAL][:5]
        audit_rows = await self._repo.recent_audit(guild_id=str(guild_id), limit=8)
        activity = [
            AuditActivity(
                action=str(row["action"]),
                target_type=str(row["target_type"]) if row["target_type"] else None,
                target_id=str(row["target_id"]) if row["target_id"] else None,
                created_at=str(row["created_at"]),
            )
            for row in audit_rows
        ]
        counts = await self._repo.mission_counts(guild_id=str(guild_id))
        return MissionControlView(
            guild_id=str(guild_id),
            counts=counts,
            active=active,
            recent=recent,
            recent_activity=activity,
            providers=dict(providers or {}),
        )

    async def recover_interrupted(self) -> int:
        """Close missions interrupted by a restart. Safe to call once at startup."""
        count = await self._repo.fail_interrupted_missions(finished_at=self._now_iso())
        if count:
            log.warning("Marked %d interrupted mission(s) as failed", count)
        return count

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ---------------------------------------------------------------- internals

    def _view(self, row: Any, step_rows: list[Any]) -> MissionView:
        result = _loads_object(row["result_json"])
        references: list[Reference] = []
        for item in result.get("references", []) if isinstance(result.get("references"), list) else []:
            if not isinstance(item, dict):
                continue
            try:
                references.append(Reference.model_validate(item))
            except ValueError:
                continue
        steps = [
            MissionStepView(
                step_id=int(step["id"]),
                index=int(step["step_index"]),
                title=str(step["title"]),
                skill_id=str(step["skill_id"]),
                status=StepStatus(str(step["status"])),
                error=str(step["error"]) if step["error"] else None,
                references=_references_from(step["references_json"]),
                output_summary=_summarize_output(str(step["skill_id"]), step["output_json"]),
                started_at=str(step["started_at"]) if step["started_at"] else None,
                finished_at=str(step["finished_at"]) if step["finished_at"] else None,
            )
            for step in step_rows
        ]
        return MissionView(
            mission_id=int(row["id"]),
            guild_id=str(row["guild_id"]),
            channel_id=str(row["channel_id"]),
            requester_id=str(row["requester_id"]),
            goal=str(row["goal"]),
            mode=MissionMode(str(row["mode"])),
            status=MissionStatus(str(row["status"])),
            trace_id=str(row["trace_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            error_code=str(row["error_code"]) if row["error_code"] else None,
            cancel_requested=bool(row["cancel_requested_at"]),
            steps=steps,
            summary=str(result.get("summary") or ""),
            references=references[:_MAX_REFERENCES],
        )

    async def _collect_references(self, view: MissionView) -> list[Reference]:
        out: list[Reference] = []
        seen: set[tuple[str, str]] = set()
        for step in view.steps:
            for reference in step.references:
                key = (reference.kind, reference.url or reference.title)
                if key in seen:
                    continue
                seen.add(key)
                out.append(reference)
                if len(out) >= _MAX_REFERENCES:
                    return out
        return out

    async def _result_for(
        self,
        view: MissionView,
        *,
        status: MissionStatus | None = None,
        error: str | None = None,
    ) -> str:
        shown_status = status or view.status
        lines = [
            f"Mission ({view.mode.value}) — {neutralize_mentions(view.goal)}",
            f"Status: {shown_status.value} · {view.progress_text()} · trace {view.trace_id}",
        ]
        for step in view.steps:
            detail = step.output_summary or step.error or "no result"
            lines.append(f"{step.index + 1}. {step.title} [{step.status.value}]: {detail}")
        lines.append(
            "Read-only mission: no message was posted, no approval was granted, and "
            "evidence is limited to the steps above."
        )
        if error:
            lines.append(f"Failure: {error}")
        references = await self._collect_references(view)
        payload = {
            "summary": truncate("\n".join(lines), _MAX_SUMMARY_CHARS),
            "references": [reference.model_dump(mode="json") for reference in references],
            "error": error,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _finalize_completed(self, guild_id: str, mission_id: int, actor_id: str) -> None:
        view = await self.get(guild_id=guild_id, mission_id=mission_id)
        result_json = await self._result_for(view, status=MissionStatus.completed)
        if await self._repo.finish_mission(
            mission_id,
            guild_id=guild_id,
            status=MissionStatus.completed.value,
            result_json=result_json,
            error_code=None,
            finished_at=self._now_iso(),
        ):
            await self._audit(
                guild_id, actor_id, mission_id, "mission.completed", {"steps": len(view.steps)}
            )

    async def _finalize_failure(
        self,
        guild_id: str,
        mission_id: int,
        actor_id: str,
        *,
        error_code: str,
        step_error: str,
    ) -> None:
        await self._repo.close_open_mission_steps(
            mission_id,
            status=StepStatus.skipped.value,
            finished_at=self._now_iso(),
            error="Skipped after a previous step failed.",
        )
        view = await self.get(guild_id=guild_id, mission_id=mission_id)
        result_json = await self._result_for(
            view, status=MissionStatus.failed, error=step_error
        )
        if await self._repo.finish_mission(
            mission_id,
            guild_id=guild_id,
            status=MissionStatus.failed.value,
            result_json=result_json,
            error_code=error_code[:64],
            finished_at=self._now_iso(),
        ):
            await self._audit(
                guild_id,
                actor_id,
                mission_id,
                "mission.failed",
                {"error_code": error_code[:64]},
            )

    async def _finalize_cancel(self, guild_id: str, mission_id: int, actor_id: str) -> None:
        await self._repo.close_open_mission_steps(
            mission_id,
            status=StepStatus.cancelled.value,
            finished_at=self._now_iso(),
            error="Cancelled.",
        )
        view = await self.get(guild_id=guild_id, mission_id=mission_id)
        result_json = await self._result_for(
            view, status=MissionStatus.cancelled, error="Cancelled by a moderator."
        )
        if await self._repo.finish_mission(
            mission_id,
            guild_id=guild_id,
            status=MissionStatus.cancelled.value,
            result_json=result_json,
            error_code="cancelled",
            finished_at=self._now_iso(),
        ):
            await self._audit(guild_id, actor_id, mission_id, "mission.cancelled", {})

    async def _audit(
        self,
        guild_id: Any,
        actor_id: Any,
        mission_id: int,
        action: str,
        meta: dict,
    ) -> None:
        with contextlib.suppress(Exception):
            await self._repo.audit(
                actor_id=str(actor_id),
                action=action,
                target_type="mission",
                target_id=str(mission_id),
                guild_id=str(guild_id),
                meta={"guild_id": str(guild_id), "mission_id": mission_id, **meta},
            )

    async def _notify(
        self,
        guild_id: str,
        mission_id: int,
        on_update: MissionCallback | None,
    ) -> None:
        if on_update is None:
            return
        try:
            await on_update(await self.get(guild_id=guild_id, mission_id=mission_id))
        except Exception as exc:  # noqa: BLE001 - UI updates never fail a mission
            log.warning("Mission progress callback failed (type=%s)", type(exc).__name__)
