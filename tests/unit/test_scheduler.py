"""Durable scheduler: idempotency, recovery, retries, timezones and isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fakes import fake_settings

from copilot.agents.contract import SkillContext, SkillPermission
from copilot.agents.host import AgentRunStatus, AgentRunView, HostMode
from copilot.core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from copilot.core.content_studio import ContentTarget, ContentType, DraftRequest
from copilot.core.scheduler import (
    ScheduledJobKind,
    SchedulerService,
)
from copilot.db.repository import Repository
from copilot.errors import (
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


class FakeStudio:
    def __init__(self) -> None:
        self.calls: list[dict] = []

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
        from types import SimpleNamespace

        return SimpleNamespace(draft_id=77)


def _service(tmp_path, repo, *, clock=None, agent=None, studio=None, **overrides):
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
        provider=object() if studio is not None else None,
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
