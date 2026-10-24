"""Durable scheduler: idempotency, recovery, retries, timezones and isolation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeProvider, fake_settings

from copilot.agents.contract import SkillContext, SkillPermission
from copilot.agents.host import AgentRunStatus, AgentRunView, HostMode
from copilot.core.approvals import ActorKind, ApprovalActor, ApprovalPermission, ApprovalService
from copilot.core.content_studio import (
    ContentStudio,
    ContentTarget,
    ContentType,
    DraftRequest,
)
from copilot.core.scheduler import (
    ScheduledJobKind,
    SchedulerService,
)
from copilot.db.repository import Repository
from copilot.errors import (
    ApprovalDeniedError,
    ContentTargetError,
    ScheduledJobNotFoundError,
    ScheduleError,
)

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
ALL = frozenset({SkillPermission.guild_member, SkillPermission.moderator})


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _context(*, guild_id: int = 500, user_id: int = 7) -> SkillContext:
    return SkillContext(
        guild_id=guild_id,
        channel_id=2002,
        parent_id=None,
        user_id=user_id,
        permissions=ALL,
    )


def _actor(*, guild_id: int = 500, actor_id: str = "7") -> ApprovalActor:
    return ApprovalActor(
        actor_id=actor_id,
        guild_id=guild_id,
        kind=ActorKind.human,
        permissions=frozenset(
            {
                ApprovalPermission.propose,
                ApprovalPermission.decide,
                ApprovalPermission.cancel,
            }
        ),
    )


def _target(*, guild_id: int = 500) -> ContentTarget:
    return ContentTarget(guild_id=guild_id, channel_id=2002, exists=True, can_send=True)


class FakeAgentService:
    def __init__(self, *, fail: bool = False, delay_s: float = 0) -> None:
        self.fail = fail
        self.delay_s = delay_s
        self.created: list[dict] = []
        self.started: list[int] = []

    async def create_run(self, context, *, goal, mode=None, idempotency_key=None):
        if self.delay_s:
            import asyncio

            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("agent exploded")
        self.created.append(
            {
                "guild_id": context.guild_id,
                "goal": goal,
                "mode": mode,
                "idempotency_key": idempotency_key,
            }
        )
        return AgentRunView(
            run_id=len(self.created),
            guild_id=str(context.guild_id),
            goal=goal,
            mode=(mode.value if mode is not None else "community"),
            status=AgentRunStatus.planned,
            trace_id="trace-1",
            created_at=NOW.isoformat(),
        )

    async def start(self, context, run_id):
        if self.fail:
            raise RuntimeError("agent start exploded")
        self.started.append(run_id)


class BlockingAgentService(FakeAgentService):
    """Agent service that parks inside ``create_run`` until released or cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create_run(self, context, *, goal, mode=None, idempotency_key=None):
        self.entered.set()
        await self.release.wait()
        return await super().create_run(
            context, goal=goal, mode=mode, idempotency_key=idempotency_key
        )


class FakeStudio:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    async def generate_draft(self, actor, request, *, target, provider, source_kind, source_ref):
        self.calls.append(
            {
                "actor": actor,
                "request": request,
                "target": target,
                "source_kind": source_kind,
                "source_ref": source_ref,
            }
        )
        if self.error is not None:
            raise self.error
        from types import SimpleNamespace

        return SimpleNamespace(draft_id=77)


class Authorizer:
    """Injected live authorization callback used by the scheduler tests."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[int, int, int]] = []

    async def __call__(self, guild_id: int, channel_id: int, requester_id: int) -> bool:
        self.calls.append((guild_id, channel_id, requester_id))
        return self.allowed


def _service(
    tmp_path,
    repo,
    *,
    clock=None,
    agent=None,
    studio=None,
    provider=None,
    authorize=None,
    **overrides,
):
    values = {
        "scheduler_enabled": True,
        "scheduler_max_attempts": 2,
        "scheduler_job_timeout_s": 5.0,
    }
    values.update(overrides)
    settings = fake_settings(tmp_path, **values)
    service = SchedulerService(
        repo,
        settings,
        agent_service=agent,
        content_studio=studio,
        provider=provider if provider is not None else (object() if studio is not None else None),
        authorize=authorize if authorize is not None else Authorizer(),
        clock=clock or Clock(),
    )
    return service


async def _schedule_agent(service, context=None, *, key="key-1", minutes=5, **kwargs):
    context = context or _context()
    run_at = (service._clock() + timedelta(minutes=minutes)).isoformat()
    return await service.schedule_agent_run(
        context,
        goal=kwargs.pop("goal", "how is the community doing"),
        run_at=run_at,
        timezone_name=kwargs.pop("timezone_name", "UTC"),
        idempotency_key=key,
        **kwargs,
    )


async def _schedule_draft(service, *, key="draft-1", minutes=5):
    request = DraftRequest(content_type=ContentType.announcement, topic="Scheduled release note")
    run_at = (service._clock() + timedelta(minutes=minutes)).isoformat()
    return await service.schedule_content_draft(
        _actor(),
        request,
        target=_target(),
        run_at=run_at,
        timezone_name="UTC",
        idempotency_key=key,
    )


# ------------------------------------------------------------------ scheduling


def test_jobs_only_cover_read_only_kinds():
    assert {kind.value for kind in ScheduledJobKind} == {
        "agent.run",
        "content.draft",
        "digest.collect",
    }
    assert all("publish" not in kind.value for kind in ScheduledJobKind)


async def test_schedule_is_idempotent_and_guild_scoped(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)

    first, created = await _schedule_agent(service)
    duplicate, created_again = await _schedule_agent(service, goal="different goal text")

    assert created is True
    assert created_again is False
    assert duplicate.job_id == first.job_id
    assert duplicate.summary == first.summary
    assert await service.list_jobs(guild_id="999") == []
    with pytest.raises(ScheduledJobNotFoundError):
        await service.get_job(guild_id="999", job_id=first.job_id)


async def test_timezone_parsing_and_rejections(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)
    context = _context()
    future = (NOW + timedelta(hours=1)).isoformat()

    job, _ = await service.schedule_agent_run(
        context,
        goal="community check",
        run_at=future,
        timezone_name="UTC",
        idempotency_key="tz-1",
    )
    assert job.run_at.endswith("+00:00")
    assert job.timezone == "UTC"

    with pytest.raises(ScheduleError):
        await service.schedule_agent_run(
            context,
            goal="community check",
            run_at=(NOW - timedelta(hours=1)).isoformat(),
            timezone_name="UTC",
            idempotency_key="tz-past",
        )
    with pytest.raises(ScheduleError):
        await service.schedule_agent_run(
            context,
            goal="community check",
            run_at=future,
            timezone_name="Not/AZone",
            idempotency_key="tz-bad",
        )
    with pytest.raises(ScheduleError):
        await service.schedule_agent_run(
            context,
            goal="community check",
            run_at="not-a-date",
            timezone_name="UTC",
            idempotency_key="tz-format",
        )


# -------------------------------------------------------------------- running


async def test_due_agent_job_starts_a_read_only_run(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent)

    job, _ = await _schedule_agent(service)
    assert await service.run_due_once() == 0  # not due yet

    clock.advance(minutes=10)
    assert await service.run_due_once() == 1

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "succeeded"
    assert agent.created[0]["goal"] == "how is the community doing"
    assert agent.created[0]["guild_id"] == 500
    assert agent.created[0]["idempotency_key"] == f"scheduled:{job.job_id}"
    assert agent.started == [1]

    rows = await tmp_db.fetchall(
        "SELECT action, guild_id FROM audit_log WHERE action LIKE 'scheduler.%'"
    )
    assert {row["action"] for row in rows} >= {"scheduler.job_created", "scheduler.job_succeeded"}
    assert all(row["guild_id"] == "500" for row in rows)


async def test_digest_job_uses_community_mode(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent)
    run_at = (NOW + timedelta(minutes=5)).isoformat()

    job, _ = await service.schedule_digest(
        _context(), run_at=run_at, timezone_name="UTC", idempotency_key="digest-1"
    )
    clock.advance(minutes=10)
    await service.run_due_once()

    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "succeeded"
    assert agent.created[0]["mode"] is HostMode.community
    assert "Community digest" in agent.created[0]["goal"]


async def test_evaluation_mode_is_authorized_persisted_recovered_and_cancelled(
    tmp_db, tmp_path
):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent)
    viewer = SkillContext(
        guild_id=500,
        channel_id=2002,
        parent_id=None,
        user_id=7,
        permissions=frozenset({SkillPermission.guild_member}),
    )
    run_at = (NOW + timedelta(minutes=5)).isoformat()
    with pytest.raises(ApprovalDeniedError):
        await service.schedule_agent_run(
            viewer,
            goal="run offline quality regression checks",
            run_at=run_at,
            timezone_name="UTC",
            mode=HostMode.evaluation,
            idempotency_key="evaluation-denied",
        )

    job, _ = await _schedule_agent(
        service,
        key="evaluation",
        goal="run offline quality regression checks",
        mode=HostMode.evaluation,
    )
    row = await repo.get_scheduled_job(job.job_id, guild_id="500")
    assert json.loads(row["payload_json"])["mode"] == HostMode.evaluation.value

    assert await repo.claim_scheduled_job(
        job.job_id, now=(NOW + timedelta(minutes=10)).isoformat()
    )
    assert await service.recover_interrupted() == 1
    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "pending"

    clock.advance(minutes=10)
    assert await service.run_due_once() == 1
    assert agent.created[0]["mode"] is HostMode.evaluation
    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "succeeded"

    cancel_me, _ = await _schedule_agent(
        service,
        key="evaluation-cancel",
        goal="run offline quality regression checks",
        mode=HostMode.evaluation,
    )
    cancelled = await service.cancel_job(
        guild_id="500", job_id=cancel_me.job_id, actor_id="7"
    )
    assert cancelled.status == "cancelled"


async def test_content_draft_job_creates_an_approval_gated_draft(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    studio = FakeStudio()
    service = _service(tmp_path, repo, clock=clock, studio=studio)
    request = DraftRequest(content_type=ContentType.announcement, topic="Scheduled release note")
    run_at = (NOW + timedelta(minutes=5)).isoformat()

    job, _ = await service.schedule_content_draft(
        _actor(),
        request,
        target=_target(),
        run_at=run_at,
        timezone_name="UTC",
        idempotency_key="draft-1",
    )
    clock.advance(minutes=10)
    assert await service.run_due_once() == 1

    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "succeeded"
    call = studio.calls[0]
    assert call["source_kind"] == "scheduled"
    assert call["source_ref"] == f"job:{job.job_id}"
    assert call["request"].topic == "Scheduled release note"
    assert call["target"].guild_id == 500


async def test_failures_retry_then_fail_with_bounded_attempts(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(tmp_path, repo, clock=clock, agent=FakeAgentService(fail=True))

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    await service.run_due_once()

    retrying = await service.get_job(guild_id="500", job_id=job.job_id)
    assert retrying.status == "pending"
    assert retrying.attempts == 1
    assert retrying.last_error == "failed"

    clock.advance(minutes=2)
    await service.run_due_once()

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "failed"
    assert final.attempts == 2
    assert final.last_error == "failed"
    rows = await tmp_db.fetchall(
        "SELECT action, guild_id FROM audit_log WHERE action = 'scheduler.job_failed'"
    )
    assert len(rows) == 1
    assert rows[0]["guild_id"] == "500"


async def test_job_timeout_is_reported_and_retried(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(
        tmp_path,
        repo,
        clock=clock,
        agent=FakeAgentService(delay_s=0.5),
        scheduler_job_timeout_s=0.01,
    )

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    await service.run_due_once()

    retrying = await service.get_job(guild_id="500", job_id=job.job_id)
    assert retrying.status == "pending"
    assert retrying.last_error == "timeout"


async def test_cancel_during_execution_cancels_the_task_without_success(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = BlockingAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent)

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    tick = asyncio.create_task(service.run_due_once())
    await asyncio.wait_for(agent.entered.wait(), timeout=1.0)

    cancelled = await service.cancel_job(
        guild_id="500", job_id=job.job_id, actor_id="7"
    )
    assert cancelled.status == "cancelled"
    agent.release.set()
    assert await asyncio.wait_for(tick, timeout=1.0) == 1

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "cancelled"
    assert agent.created == []
    actions = {
        row["action"]
        for row in await tmp_db.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'scheduler.%'"
        )
    }
    assert "scheduler.job_cancelled" in actions
    assert "scheduler.job_succeeded" not in actions
    assert "scheduler.job_failed" not in actions


async def test_success_cannot_overwrite_a_cancelled_row(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent)

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    now = clock().isoformat()
    row = await repo.get_scheduled_job(job.job_id, guild_id="500")
    assert row is not None
    assert await repo.claim_scheduled_job(job.job_id, now=now)
    # Cancellation wins while dispatch is still in flight.
    assert await repo.cancel_scheduled_job(job.job_id, guild_id="500", now=now)

    await service._execute(row)

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "cancelled"
    assert agent.created  # the dispatch itself still ran
    actions = {
        row["action"]
        for row in await tmp_db.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'scheduler.%'"
        )
    }
    assert "scheduler.job_succeeded" not in actions


async def test_failure_cannot_overwrite_a_cancelled_row(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(tmp_path, repo, clock=clock, agent=FakeAgentService(fail=True))

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    now = clock().isoformat()
    row = await repo.get_scheduled_job(job.job_id, guild_id="500")
    assert row is not None
    assert await repo.claim_scheduled_job(job.job_id, now=now)
    assert await repo.cancel_scheduled_job(job.job_id, guild_id="500", now=now)

    await service._execute(row)

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "cancelled"
    assert final.last_error is None
    actions = {
        row["action"]
        for row in await tmp_db.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'scheduler.%'"
        )
    }
    assert "scheduler.job_failed" not in actions
    assert "scheduler.job_retry_scheduled" not in actions


async def test_cancel_and_restart_recovery(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent)

    cancel_me, _ = await _schedule_agent(service, key="cancel-me")
    recover_me, _ = await _schedule_agent(service, key="recover-me")

    cancelled = await service.cancel_job(
        guild_id="500", job_id=cancel_me.job_id, actor_id="7"
    )
    assert cancelled.status == "cancelled"

    # Simulate a crash after claim: the job is running with no process.
    assert await repo.claim_scheduled_job(
        recover_me.job_id, now=(NOW + timedelta(minutes=10)).isoformat()
    )
    assert await service.recover_interrupted() == 1
    recovered = await service.get_job(guild_id="500", job_id=recover_me.job_id)
    assert recovered.status == "pending"
    assert recovered.last_error == "interrupted by restart"

    clock.advance(minutes=10)
    assert await service.run_due_once() == 1
    assert agent.started == [1]


async def test_guild_outside_allowlist_fails_without_running(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    service = _service(tmp_path, repo, clock=clock, agent=agent, guild_allowlist=[500])

    job, _ = await _schedule_agent(service, _context(guild_id=999), key="foreign")
    clock.advance(minutes=10)
    await service.run_due_once()

    final = await service.get_job(guild_id="999", job_id=job.job_id)
    assert final.status == "failed"
    assert final.last_error == "guild_not_allowed"
    assert agent.created == []


async def test_revoked_moderator_is_a_permanent_failure_without_agent_work(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    authorizer = Authorizer(allowed=False)
    service = _service(tmp_path, repo, clock=clock, agent=agent, authorize=authorizer)

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    assert await service.run_due_once() == 1

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "failed"
    assert final.last_error == "authorization_revoked"
    assert final.attempts == 1
    assert agent.created == []
    assert authorizer.calls == [(500, 2002, 7)]

    rows = await tmp_db.fetchall(
        "SELECT guild_id, meta_json FROM audit_log "
        "WHERE action = 'scheduler.job_authorization_revoked'"
    )
    assert len(rows) == 1
    assert rows[0]["guild_id"] == "500"
    meta = json.loads(rows[0]["meta_json"])
    assert meta["guild_id"] == "500"
    assert meta["error_code"] == "authorization_revoked"
    assert meta["requester_id"] == "7"


async def test_transient_authorization_lookup_failure_is_retryable(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()

    async def unavailable(*_args):
        raise RuntimeError("gateway cache not ready")

    service = _service(tmp_path, repo, clock=clock, agent=agent, authorize=unavailable)
    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)

    assert await service.run_due_once() == 1
    retrying = await service.get_job(guild_id="500", job_id=job.job_id)
    assert retrying.status == "pending"
    assert retrying.last_error == "authorization_temporarily_unavailable"
    assert agent.created == []


async def test_channel_revoked_after_scheduling_blocks_execution(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    authorizer = Authorizer()
    service = _service(
        tmp_path,
        repo,
        clock=clock,
        agent=agent,
        authorize=authorizer,
        channel_allowlist=[9999],
    )

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    assert await service.run_due_once() == 1

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "failed"
    assert final.last_error == "channel_not_allowed"
    assert agent.created == []
    assert authorizer.calls == []
    rows = await tmp_db.fetchall(
        "SELECT guild_id FROM audit_log WHERE action = 'scheduler.job_authorization_revoked'"
    )
    assert [row["guild_id"] for row in rows] == ["500"]


async def test_missing_authorizer_fails_closed(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    agent = FakeAgentService()
    settings = fake_settings(
        tmp_path,
        scheduler_enabled=True,
        scheduler_max_attempts=2,
        scheduler_job_timeout_s=5.0,
    )
    service = SchedulerService(
        repo, settings, agent_service=agent, clock=clock
    )

    job, _ = await _schedule_agent(service)
    clock.advance(minutes=10)
    await service.run_due_once()

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "failed"
    assert final.last_error == "authorization_unavailable"
    assert agent.created == []


async def test_recovery_respects_attempt_limit_and_clears_timestamps(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(tmp_path, repo, clock=clock)
    job, _ = await _schedule_agent(service, key="exhausted")
    claimed_at = (NOW + timedelta(minutes=10)).isoformat()
    assert await repo.claim_scheduled_job(job.job_id, now=claimed_at)
    await tmp_db.execute(
        "UPDATE scheduled_jobs SET attempts = max_attempts WHERE id = ?",
        (job.job_id,),
    )

    assert await service.recover_interrupted() == 0
    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "failed"
    assert final.last_error == "interrupted by restart"
    assert final.attempts == final.max_attempts
    assert final.started_at is not None
    assert final.finished_at is not None
    assert await service.recover_interrupted() == 0

    row = await tmp_db.fetchone(
        "SELECT action, guild_id FROM audit_log WHERE action = 'scheduler.job_failed'"
    )
    assert row is not None
    assert row["guild_id"] == "500"


async def test_recovery_resets_stale_execution_timestamps(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(tmp_path, repo, clock=clock)
    job, _ = await _schedule_agent(service, key="recover-twice")
    claimed_at = (NOW + timedelta(minutes=10)).isoformat()
    assert await repo.claim_scheduled_job(job.job_id, now=claimed_at)

    assert await service.recover_interrupted() == 1
    first = await service.get_job(guild_id="500", job_id=job.job_id)
    assert first.status == "pending"
    assert first.started_at is None
    assert first.finished_at is None
    assert await service.recover_interrupted() == 0
    second = await service.get_job(guild_id="500", job_id=job.job_id)
    assert second.status == "pending"
    assert second.started_at is None


async def test_recovery_runs_when_scheduler_is_disabled(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    scheduler = _service(tmp_path, repo, clock=clock)
    job, _ = await _schedule_agent(scheduler, key="disabled-recovery")
    claimed_at = (NOW + timedelta(minutes=10)).isoformat()
    assert await repo.claim_scheduled_job(job.job_id, now=claimed_at)

    disabled = _service(tmp_path, repo, clock=clock, scheduler_enabled=False)
    await disabled.start()
    assert disabled._task is None
    recovered = await disabled.get_job(guild_id="500", job_id=job.job_id)
    assert recovered.status == "pending"
    assert recovered.started_at is None


async def test_claim_does_not_exceed_the_attempt_limit(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(tmp_path, repo, clock=clock)
    job, _ = await _schedule_agent(service, key="capped")
    await tmp_db.execute(
        "UPDATE scheduled_jobs SET attempts = max_attempts WHERE id = ?",
        (job.job_id,),
    )
    due_at = (NOW + timedelta(minutes=10)).isoformat()
    assert await repo.claim_scheduled_job(job.job_id, now=due_at) is False
    unchanged = await service.get_job(guild_id="500", job_id=job.job_id)
    assert unchanged.status == "pending"
    assert unchanged.attempts == unchanged.max_attempts


async def test_draft_target_revoked_is_permanent(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    studio = FakeStudio(error=ContentTargetError("target no longer writable"))
    service = _service(tmp_path, repo, clock=clock, studio=studio)

    job, _ = await _schedule_draft(service)
    clock.advance(minutes=10)
    await service.run_due_once()

    final = await service.get_job(guild_id="500", job_id=job.job_id)
    assert final.status == "failed"
    assert final.last_error == "content_target_revoked"
    assert final.attempts == 1


async def test_transient_draft_failure_remains_retryable(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    studio = FakeStudio(error=RuntimeError("model unavailable"))
    service = _service(tmp_path, repo, clock=clock, studio=studio)

    job, _ = await _schedule_draft(service)
    clock.advance(minutes=10)
    await service.run_due_once()

    retrying = await service.get_job(guild_id="500", job_id=job.job_id)
    assert retrying.status == "pending"
    assert retrying.last_error == "content_generation_failed"
    assert retrying.attempts == 1



async def test_calendar_link_cancels_the_linked_job(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    clock = Clock()
    service = _service(tmp_path, repo, clock=clock, agent=FakeAgentService())
    entry_id = await repo.insert_calendar_entry(
        guild_id="500",
        title="Release reminder",
        kind="reminder",
        starts_at=(NOW + timedelta(days=1)).isoformat(),
        ends_at=None,
        target_channel_id="2002",
        draft_id=None,
        event_plan_id=None,
        notes="",
        created_by="7",
        created_at=NOW.isoformat(),
    )
    job, _ = await _schedule_agent(service, key="calendar-1")
    assert await repo.link_calendar_entry_job(
        entry_id, guild_id="500", job_id=job.job_id, updated_at=NOW.isoformat()
    )

    assert await service.cancel_for_calendar_entry(
        guild_id="500", entry_id=entry_id, actor_id="7"
    )
    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "cancelled"


async def test_crash_after_draft_insertion_reuses_the_same_draft(tmp_db, tmp_path):
    """The draft exists but the job row never recorded success; a retry reuses it."""
    repo = Repository(tmp_db)
    clock = Clock()
    settings = fake_settings(
        tmp_path,
        scheduler_enabled=True,
        scheduler_max_attempts=3,
        scheduler_job_timeout_s=5.0,
    )
    studio = ContentStudio(repo, ApprovalService(repo), settings=settings)
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "content": "The scheduled release note for the community.",
                    "needs_research": False,
                    "research_notes": "",
                    "warnings": [],
                }
            )
        ]
    )
    service = _service(
        tmp_path,
        repo,
        clock=clock,
        studio=studio,
        provider=provider,
        scheduler_max_attempts=3,
    )

    job, _ = await _schedule_draft(service)
    clock.advance(minutes=10)
    assert await service.run_due_once() == 1
    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "succeeded"

    # Simulate a crash after the draft insert but before the job row was finalised.
    retry_at = (clock() + timedelta(minutes=10)).isoformat()
    await tmp_db.execute(
        "UPDATE scheduled_jobs SET status = 'pending', attempts = 0, run_at = ? WHERE id = ?",
        (retry_at, job.job_id),
    )
    clock.advance(minutes=10)
    assert await service.run_due_once() == 1

    assert (await service.get_job(guild_id="500", job_id=job.job_id)).status == "succeeded"
    assert len(provider.calls) == 1
    rows = await tmp_db.fetchall(
        "SELECT id FROM content_drafts WHERE guild_id = '500' AND source_ref = ?",
        (f"job:{job.job_id}",),
    )
    assert len(rows) == 1
