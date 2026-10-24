"""Durable scheduler for read-only analysis and human-approved draft preparation.

Jobs are persisted in SQLite and survive restarts. The scheduler never sends a
Discord message, publishes content, creates a Discord event, or performs a
moderation action: it can only start a read-only host-agent run or generate a
Content Studio draft, which still requires explicit human approval before any
publication. Scheduled public publishing is not a job kind at all.

Delivery guarantees are intentionally bounded: a job is claimed atomically,
failed jobs retry with backoff up to ``scheduler_max_attempts``, interruption by
restart returns jobs to pending, and every transition is guild-scoped in audit.

Cancellation is cooperative. The runner keeps the in-flight asyncio task for a
job, so ``/schedule-cancel`` both marks the row cancelled and cancels the task.
Finalisation is conditional on the row still being ``running``: a cancellation
that wins the race can never afterwards be overwritten by (or audited as) a
success or failure. If the process stops with a job still running, the row is
left untouched and startup recovery returns it to pending.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, ValidationError

from ..agents.contract import SkillContext, SkillPermission
from ..agents.host import AgentRunService, AgentRunStatus, HostMode
from ..core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from ..core.content_studio import ContentTarget, DraftRequest
from ..db.repository import Repository
from ..errors import (
    AgentPlanError,
    ApprovalDeniedError,
    ContentTargetError,
    ScheduledJobConflictError,
    ScheduledJobNotFoundError,
    ScheduleError,
)
from ..llm.base import LLMProvider
from ..ui.checks import channel_allowed, guild_allowed
from ..util.text import sanitize_for_prompt, strip_control_tokens, truncate

log = logging.getLogger(__name__)

MAX_GOAL_CHARS = 500
MAX_IDEMPOTENCY_CHARS = 100
MAX_PAYLOAD_CHARS = 12_000
_ALL_PERMISSIONS = frozenset({SkillPermission.guild_member, SkillPermission.moderator})
_TERMINAL = {"succeeded", "failed", "cancelled"}
_PERMANENT_FAILURES = {
    "guild_not_allowed",
    "channel_not_allowed",
    "authorization_revoked",
    "authorization_unavailable",
    "content_target_revoked",
    "invalid_payload",
    "unsupported_kind",
}
_AUTHORIZATION_FAILURES = {
    "guild_not_allowed",
    "channel_not_allowed",
    "authorization_revoked",
    "authorization_unavailable",
    "content_target_revoked",
}


class ScheduledJobKind(str, Enum):
    agent_run = "agent.run"
    content_draft = "content.draft"
    digest_collect = "digest.collect"


class ScheduledJobView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    guild_id: str
    kind: ScheduledJobKind
    status: str
    run_at: str
    timezone: str
    attempts: int
    max_attempts: int
    last_error: str | None = None
    summary: str = ""
    created_by: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


class _JobFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _resolve_timezone(name: str) -> tzinfo:
    text = (name or "UTC").strip()
    if text.upper() in {"UTC", "ETC/UTC", "Z"}:
        return UTC
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleError(
            f"The timezone '{truncate(text, 64)}' is unavailable on this host. "
            "Use UTC or an ISO timestamp with an explicit offset, or install tzdata."
        ) from exc


def _job_summary(kind: str, payload: dict[str, Any]) -> str:
    if kind == ScheduledJobKind.content_draft.value:
        request = payload.get("request")
        if isinstance(request, dict):
            topic = sanitize_for_prompt(str(request.get("topic") or ""))
            content_type = sanitize_for_prompt(str(request.get("content_type") or "draft"))
            return truncate(f"{content_type}: {topic}", 300)
        return "content draft"
    goal = payload.get("goal")
    return truncate(sanitize_for_prompt(str(goal or "")), 300) if goal else kind


class SchedulerService:
    """Persist, run, retry, cancel and recover durable guild-scoped jobs."""

    def __init__(
        self,
        repo: Repository,
        settings: Any,
        *,
        agent_service: AgentRunService | None = None,
        content_studio: Any | None = None,
        provider: LLMProvider | None = None,
        authorize: Callable[[int, int, int], Awaitable[bool]] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._agent_service = agent_service
        self._content_studio = content_studio
        self._provider = provider
        # Injected live authorization check: (guild_id, channel_id, requester_id).
        # Stored payload permissions are never trusted at execution time.
        self._authorize = authorize
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        # Recovery always runs, even when scheduling is disabled: a restart
        # must never leave a job stuck in 'running'. When disabled the recovered
        # jobs stay pending until an operator enables the scheduler.
        recovered = await self.recover_interrupted()
        if recovered:
            log.warning("Recovered %d interrupted scheduled job(s)", recovered)
        if not self._settings.scheduler_enabled:
            return
        self._task = asyncio.create_task(self._loop(), name="copilot-scheduler")

    async def stop(self) -> None:
        loop_task, self._task = self._task, None
        if loop_task is not None:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        # Cancel in-flight work but do not rewrite its row: shutdown recovery
        # returns every still-running job to pending on the next start.
        running = list(self._running.values())
        for job_task in running:
            job_task.cancel()
        if running:
            await asyncio.wait(running, timeout=5.0)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due_once()
            except Exception:  # noqa: BLE001 - the scheduler loop must never die
                log.exception("Scheduler tick failed")
            await asyncio.sleep(float(self._settings.scheduler_tick_s))

    async def recover_interrupted(self) -> int:
        return await self._repo.recover_interrupted_scheduled_jobs(now=_iso(self._clock()))

    # ------------------------------------------------------------- scheduling

    async def schedule_agent_run(
        self,
        context: SkillContext,
        *,
        goal: str,
        run_at: str,
        timezone_name: str,
        mode: HostMode | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ScheduledJobView, bool]:
        if SkillPermission.moderator not in context.permissions:
            raise ApprovalDeniedError()
        clean_goal = truncate(
            strip_control_tokens(goal or "").strip(), MAX_GOAL_CHARS
        )
        if len(clean_goal) < 3:
            raise ScheduleError("The goal must be at least 3 characters.")
        payload = {
            "goal": clean_goal,
            "mode": mode.value if mode is not None else None,
            "channel_id": str(context.channel_id),
            "parent_id": None if context.parent_id is None else str(context.parent_id),
            "requester_id": str(context.user_id),
        }
        return await self._insert(
            guild_id=str(context.guild_id),
            kind=ScheduledJobKind.agent_run,
            payload=payload,
            run_at=run_at,
            timezone_name=timezone_name,
            created_by=str(context.user_id),
            idempotency_key=idempotency_key or f"agent:{uuid.uuid4().hex}",
        )

    async def schedule_digest(
        self,
        context: SkillContext,
        *,
        run_at: str,
        timezone_name: str,
        goal: str = "",
        idempotency_key: str | None = None,
    ) -> tuple[ScheduledJobView, bool]:
        if SkillPermission.moderator not in context.permissions:
            raise ApprovalDeniedError()
        clean_goal = truncate(
            strip_control_tokens(goal or "").strip(), MAX_GOAL_CHARS
        ) or "Community digest: summarize pulse, insights and approved knowledge"
        payload = {
            "goal": clean_goal,
            "mode": HostMode.community.value,
            "channel_id": str(context.channel_id),
            "parent_id": None if context.parent_id is None else str(context.parent_id),
            "requester_id": str(context.user_id),
        }
        return await self._insert(
            guild_id=str(context.guild_id),
            kind=ScheduledJobKind.digest_collect,
            payload=payload,
            run_at=run_at,
            timezone_name=timezone_name,
            created_by=str(context.user_id),
            idempotency_key=idempotency_key or f"digest:{uuid.uuid4().hex}",
        )

    async def schedule_content_draft(
        self,
        actor: ApprovalActor,
        request: DraftRequest,
        *,
        target: ContentTarget,
        run_at: str,
        timezone_name: str,
        idempotency_key: str | None = None,
    ) -> tuple[ScheduledJobView, bool]:
        if ApprovalPermission.propose not in actor.permissions:
            raise ApprovalDeniedError()
        if target.guild_id != actor.guild_id:
            raise ScheduleError("The draft target must be inside this server.")
        payload = {
            "request": request.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
            "requester_id": actor.actor_id,
        }
        return await self._insert(
            guild_id=str(actor.guild_id),
            kind=ScheduledJobKind.content_draft,
            payload=payload,
            run_at=run_at,
            timezone_name=timezone_name,
            created_by=actor.actor_id,
            idempotency_key=idempotency_key or f"draft:{uuid.uuid4().hex}",
        )

    async def _insert(
        self,
        *,
        guild_id: str,
        kind: ScheduledJobKind,
        payload: dict[str, Any],
        run_at: str,
        timezone_name: str,
        created_by: str,
        idempotency_key: str,
    ) -> tuple[ScheduledJobView, bool]:
        if not self._settings.scheduler_enabled:
            raise ScheduleError("Scheduling is disabled on this bot.")
        clean_key = truncate(strip_control_tokens(idempotency_key or "").strip(), MAX_IDEMPOTENCY_CHARS)
        if len(clean_key) < 3:
            raise ScheduleError("The scheduling idempotency key is invalid.")
        zone = _resolve_timezone(timezone_name)
        moment = self._parse_run_at(run_at, zone)
        now = self._clock()
        if moment <= now:
            raise ScheduleError("The scheduled time must be in the future.")
        payload_json = json.dumps(payload, ensure_ascii=False)
        if len(payload_json) > MAX_PAYLOAD_CHARS:
            raise ScheduleError("The scheduled payload is too large.")
        job_id, created = await self._repo.insert_scheduled_job(
            guild_id=str(guild_id),
            job_kind=kind.value,
            payload_json=payload_json,
            run_at=_iso(moment),
            timezone=str(getattr(zone, "key", "UTC")),
            max_attempts=int(self._settings.scheduler_max_attempts),
            idempotency_key=clean_key,
            created_by=str(created_by),
            now=_iso(now),
        )
        if created:
            await self._repo.audit(
                actor_id=str(created_by),
                action="scheduler.job_created",
                target_type="scheduled_job",
                target_id=str(job_id),
                guild_id=str(guild_id),
                meta={
                    "guild_id": str(guild_id),
                    "kind": kind.value,
                    "run_at": _iso(moment),
                    "timezone": str(getattr(zone, "key", "UTC")),
                },
            )
        row = await self._repo.get_scheduled_job(job_id, guild_id=str(guild_id))
        if row is None:
            raise ScheduledJobNotFoundError()
        return self._view(row), created

    @staticmethod
    def _parse_run_at(raw: str, zone: tzinfo) -> datetime:
        text = (raw or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ScheduleError(
                "The scheduled time must be an ISO date/time, for example 2026-11-05T18:00."
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        # A nonexistent local time (DST gap) still resolves deterministically to
        # the zone's offset; ambiguous times use the earlier offset.
        return parsed.astimezone(UTC)

    # --------------------------------------------------------------- queries

    async def get_job(self, *, guild_id: str, job_id: int) -> ScheduledJobView:
        row = await self._repo.get_scheduled_job(job_id, guild_id=str(guild_id))
        if row is None:
            raise ScheduledJobNotFoundError()
        return self._view(row)

    async def list_jobs(
        self, *, guild_id: str, status: str | None = None, limit: int = 20
    ) -> list[ScheduledJobView]:
        rows = await self._repo.list_scheduled_jobs(
            guild_id=str(guild_id), status=status, limit=limit
        )
        return [self._view(row) for row in rows]

    async def cancel_job(
        self, *, guild_id: str, job_id: int, actor_id: str
    ) -> ScheduledJobView:
        row = await self._repo.get_scheduled_job(job_id, guild_id=str(guild_id))
        if row is None:
            raise ScheduledJobNotFoundError()
        if str(row["status"]) in _TERMINAL:
            return self._view(row)
        if not await self._repo.cancel_scheduled_job(
            job_id, guild_id=str(guild_id), now=_iso(self._clock())
        ):
            # Another transition (typically completion) won the race. Report
            # the real terminal state instead of claiming a cancellation.
            refreshed = await self._repo.get_scheduled_job(job_id, guild_id=str(guild_id))
            if refreshed is None:
                raise ScheduledJobNotFoundError()
            if str(refreshed["status"]) in _TERMINAL:
                return self._view(refreshed)
            raise ScheduledJobConflictError()
        await self._repo.audit(
            actor_id=str(actor_id),
            action="scheduler.job_cancelled",
            target_type="scheduled_job",
            target_id=str(job_id),
            guild_id=str(guild_id),
            meta={"guild_id": str(guild_id), "kind": str(row["job_kind"])},
        )
        await self._cancel_running_task(job_id)
        updated = await self._repo.get_scheduled_job(job_id, guild_id=str(guild_id))
        if updated is None:
            raise ScheduledJobNotFoundError()
        return self._view(updated)

    async def cancel_for_calendar_entry(
        self, *, guild_id: str, entry_id: int, actor_id: str
    ) -> bool:
        entry = await self._repo.get_calendar_entry(entry_id, guild_id=str(guild_id))
        if entry is None or entry["scheduled_job_id"] is None:
            return False
        job_id = int(entry["scheduled_job_id"])
        if not await self._repo.cancel_scheduled_job(
            job_id, guild_id=str(guild_id), now=_iso(self._clock())
        ):
            return False
        await self._repo.audit(
            actor_id=str(actor_id),
            action="scheduler.job_cancelled",
            target_type="scheduled_job",
            target_id=str(job_id),
            guild_id=str(guild_id),
            meta={"guild_id": str(guild_id), "calendar_entry_id": int(entry_id)},
        )
        await self._cancel_running_task(job_id)
        return True

    async def _cancel_running_task(self, job_id: int) -> None:
        """Cancel one in-flight job task and wait briefly for it to unwind.

        The row has already been marked cancelled by the caller, so a task that
        ignores cancellation for a while still cannot finalise as success.
        """
        task = self._running.get(int(job_id))
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=5.0)

    # ----------------------------------------------------------------- runner

    async def run_due_once(self) -> int:
        now = _iso(self._clock())
        rows = await self._repo.due_scheduled_jobs(
            now, limit=int(self._settings.scheduler_tick_batch)
        )
        tasks: list[asyncio.Task] = []
        for row in rows:
            job_id = int(row["id"])
            if not await self._repo.claim_scheduled_job(job_id, now=now):
                continue
            task = asyncio.create_task(
                self._run_claimed(row), name=f"copilot-scheduled-job-{job_id}"
            )
            self._running[job_id] = task
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def _run_claimed(self, row: Any) -> None:
        job_id = int(row["id"])
        try:
            await self._execute(row)
        finally:
            self._running.pop(job_id, None)

    async def _execute(self, row: Any) -> None:
        job_id = int(row["id"])
        guild_id = str(row["guild_id"])
        kind = str(row["job_kind"])
        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])
        try:
            payload = self._payload(row)
        except _JobFailure as exc:
            await self._fail_or_retry(job_id, guild_id, kind, attempts, max_attempts, exc.code)
            return
        try:
            await self._authorize_execution(row, payload)
        except _JobFailure as exc:
            if exc.code in _AUTHORIZATION_FAILURES:
                await self._audit_authorization_revoked(job_id, guild_id, kind, payload, exc.code)
            await self._fail_or_retry(job_id, guild_id, kind, attempts, max_attempts, exc.code)
            return
        try:
            async with asyncio.timeout(float(self._settings.scheduler_job_timeout_s)):
                reference = await self._dispatch(row, payload)
        except asyncio.CancelledError:
            # Cancellation is owned by cancel_job/stop: the row is already
            # cancelled (or will be recovered after restart). Never finalise.
            raise
        except TimeoutError:
            await self._fail_or_retry(
                job_id, guild_id, kind, attempts, max_attempts, "timeout"
            )
        except _JobFailure as exc:
            if exc.code in _AUTHORIZATION_FAILURES:
                await self._audit_authorization_revoked(job_id, guild_id, kind, payload, exc.code)
            await self._fail_or_retry(
                job_id, guild_id, kind, attempts, max_attempts, exc.code
            )
        except Exception:  # noqa: BLE001 - one job must not stop the batch
            log.exception("Scheduled job %s failed", job_id)
            await self._fail_or_retry(
                job_id, guild_id, kind, attempts, max_attempts, "failed"
            )
        else:
            finished = _iso(self._clock())
            if not await self._repo.finish_scheduled_job(
                job_id, status="succeeded", last_error=None, finished_at=finished
            ):
                # A cancellation (or another transition) won the race; the row
                # must not be audited as a success.
                log.info("Scheduled job %s finished after losing a state race", job_id)
                return
            await self._repo.audit(
                actor_id=str(row["created_by"]),
                action="scheduler.job_succeeded",
                target_type="scheduled_job",
                target_id=str(job_id),
                guild_id=guild_id,
                meta={
                    "guild_id": guild_id,
                    "kind": kind,
                    "attempts": attempts,
                    "result": truncate(reference, 120),
                },
            )

    async def _authorize_execution(self, row: Any, payload: dict[str, Any]) -> None:
        """Revalidate allowlists and live moderator status before any work.

        The payload's stored permissions are informational only: the requester
        must still be authorised right now, through the injected callback.
        """
        guild_id = int(str(row["guild_id"]))
        kind = str(row["job_kind"])
        try:
            if kind == ScheduledJobKind.content_draft.value:
                target = payload["target"]
                if not isinstance(target, dict):
                    raise TypeError("target must be an object")
                target_guild_id = int(target["guild_id"])
                channel_id = int(target["channel_id"])
                parent_id = None
                requester_id = int(payload["requester_id"])
                if target_guild_id != guild_id:
                    raise ValueError("target guild mismatch")
            else:
                channel_id = int(payload["channel_id"])
                requester_id = int(payload["requester_id"])
                parent = payload.get("parent_id")
                parent_id = None if parent is None else int(parent)
        except (KeyError, TypeError, ValueError) as exc:
            raise _JobFailure("invalid_payload") from exc

        if not guild_allowed(self._settings.guild_allowlist, guild_id):
            raise _JobFailure("guild_not_allowed")
        if not channel_allowed(self._settings.channel_allowlist, channel_id, parent_id):
            raise _JobFailure("channel_not_allowed")
        if self._authorize is None:
            # Fail closed: without a live check the stored requester state
            # cannot be trusted.
            raise _JobFailure("authorization_unavailable")
        try:
            allowed = await self._authorize(guild_id, channel_id, requester_id)
        except Exception:  # noqa: BLE001 - denial must fail closed
            log.warning("Scheduled job authorization check failed (job=%s)", int(row["id"]))
            # A transient REST/cache failure is retryable; a confirmed missing
            # member returns False below and stays a permanent revocation.
            raise _JobFailure("authorization_temporarily_unavailable") from None
        if not allowed:
            raise _JobFailure("authorization_revoked")

    async def _audit_authorization_revoked(
        self,
        job_id: int,
        guild_id: str,
        kind: str,
        payload: dict[str, Any],
        error_code: str,
    ) -> None:
        meta = {
            "guild_id": guild_id,
            "kind": kind,
            "error_code": error_code,
        }
        target = payload.get("target")
        if isinstance(target, dict):
            meta["channel_id"] = str(target.get("channel_id") or "")
        elif payload.get("channel_id") is not None:
            meta["channel_id"] = str(payload.get("channel_id"))
        if payload.get("requester_id") is not None:
            meta["requester_id"] = str(payload.get("requester_id"))
        await self._repo.audit(
            actor_id="scheduler",
            action="scheduler.job_authorization_revoked",
            target_type="scheduled_job",
            target_id=str(job_id),
            guild_id=guild_id,
            meta=meta,
        )

    async def _fail_or_retry(
        self,
        job_id: int,
        guild_id: str,
        kind: str,
        attempts: int,
        max_attempts: int,
        code: str,
    ) -> None:
        now = self._clock()
        if attempts < max_attempts and code not in _PERMANENT_FAILURES:
            delay_s = min(300, 30 * (2 ** max(0, attempts - 1)))
            next_run = _iso(now + timedelta(seconds=delay_s))
            if await self._repo.reschedule_scheduled_job(
                job_id, run_at=next_run, last_error=code, now=_iso(now)
            ):
                await self._repo.audit(
                    actor_id="scheduler",
                    action="scheduler.job_retry_scheduled",
                    target_type="scheduled_job",
                    target_id=str(job_id),
                    guild_id=guild_id,
                    meta={
                        "guild_id": guild_id,
                        "kind": kind,
                        "attempts": attempts,
                        "error_code": code,
                        "next_run_at": next_run,
                    },
                )
                return
        if not await self._repo.finish_scheduled_job(
            job_id, status="failed", last_error=code, finished_at=_iso(now)
        ):
            # Cancelled (or finalised elsewhere) while this attempt was unwinding.
            log.info("Scheduled job %s failure not recorded after a state race", job_id)
            return
        await self._repo.audit(
            actor_id="scheduler",
            action="scheduler.job_failed",
            target_type="scheduled_job",
            target_id=str(job_id),
            guild_id=guild_id,
            meta={
                "guild_id": guild_id,
                "kind": kind,
                "attempts": attempts,
                "error_code": code,
            },
        )

    async def _dispatch(self, row: Any, payload: dict[str, Any]) -> str:
        kind = str(row["job_kind"])
        job_id = int(row["id"])
        guild_id = str(row["guild_id"])
        if kind in (ScheduledJobKind.agent_run.value, ScheduledJobKind.digest_collect.value):
            return await self._dispatch_agent(row, payload, job_id=job_id, guild_id=guild_id)
        if kind == ScheduledJobKind.content_draft.value:
            return await self._dispatch_draft(payload, job_id=job_id, guild_id=guild_id)
        raise _JobFailure("unsupported_kind")

    async def _dispatch_agent(
        self, row: Any, payload: dict[str, Any], *, job_id: int, guild_id: str
    ) -> str:
        if self._agent_service is None:
            raise _JobFailure("agent_unavailable")
        try:
            channel_id = int(payload["channel_id"])
            requester_id = int(payload["requester_id"])
            parent_id = payload.get("parent_id")
            mode = HostMode(str(payload["mode"])) if payload.get("mode") else None
            goal = str(payload.get("goal") or "")
        except (KeyError, TypeError, ValueError) as exc:
            raise _JobFailure("invalid_payload") from exc
        context = SkillContext(
            guild_id=int(guild_id),
            channel_id=channel_id,
            parent_id=None if parent_id is None else int(parent_id),
            user_id=requester_id,
            permissions=_ALL_PERMISSIONS,
        )
        try:
            view = await self._agent_service.create_run(
                context,
                goal=goal,
                mode=mode,
                idempotency_key=f"scheduled:{job_id}",
            )
            if view.status is AgentRunStatus.planned:
                await self._agent_service.start(context, view.run_id)
        except AgentPlanError as exc:
            raise _JobFailure("invalid_payload") from exc
        return f"agent_run:{view.run_id}"

    async def _dispatch_draft(
        self, payload: dict[str, Any], *, job_id: int, guild_id: str
    ) -> str:
        if self._content_studio is None or self._provider is None:
            raise _JobFailure("content_generation_unavailable")
        try:
            request = DraftRequest.model_validate(payload["request"])
            target = ContentTarget.model_validate(payload["target"])
            requester_id = str(payload["requester_id"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise _JobFailure("invalid_payload") from exc
        if target.guild_id != int(guild_id):
            raise _JobFailure("invalid_payload")
        actor = ApprovalActor(
            actor_id=requester_id,
            guild_id=int(guild_id),
            kind=ActorKind.human,
            permissions=frozenset({ApprovalPermission.propose}),
        )
        try:
            draft = await self._content_studio.generate_draft(
                actor,
                request,
                target=target,
                provider=self._provider,
                source_kind="scheduled",
                source_ref=f"job:{job_id}",
            )
        except ApprovalDeniedError as exc:
            # Guild allowlist or target guild changed after the job was queued.
            log.warning("Scheduled draft authorization revoked (job=%s)", job_id)
            raise _JobFailure("authorization_revoked") from exc
        except ContentTargetError as exc:
            # Target channel removed from the allowlist, deleted, or unwritable.
            log.warning("Scheduled draft target revoked (job=%s)", job_id)
            raise _JobFailure("content_target_revoked") from exc
        except Exception as exc:  # noqa: BLE001 - classification only, no text persisted
            log.warning("Scheduled draft generation failed (job=%s): %s", job_id, type(exc).__name__)
            raise _JobFailure("content_generation_failed") from exc
        return f"content_draft:{draft.draft_id}"

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _payload(row: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise _JobFailure("invalid_payload") from exc
        if not isinstance(parsed, dict):
            raise _JobFailure("invalid_payload")
        return parsed

    @staticmethod
    def _view(row: Any) -> ScheduledJobView:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            payload = {}
        kind = str(row["job_kind"])
        return ScheduledJobView(
            job_id=int(row["id"]),
            guild_id=str(row["guild_id"]),
            kind=ScheduledJobKind(kind),
            status=str(row["status"]),
            run_at=str(row["run_at"]),
            timezone=str(row["timezone"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            summary=_job_summary(kind, payload if isinstance(payload, dict) else {}),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
        )
