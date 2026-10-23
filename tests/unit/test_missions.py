"""Missions: deterministic plans, bounded execution, progress, failure and cancel."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fakes import fake_settings

from copilot.agents.builtin import InsightsSkill, KbSearchSkill, PulseSkill
from copilot.agents.contract import SkillContext, SkillPermission
from copilot.agents.orchestrator import SkillOrchestrator
from copilot.agents.registry import SkillRegistry
from copilot.db.repository import Repository
from copilot.errors import (
    MissionConflictError,
    MissionNotFoundError,
    MissionPlanError,
    NotModeratorError,
)
from copilot.missions.models import MissionMode, MissionStatus, StepStatus
from copilot.missions.planner import build_mission_plan
from copilot.missions.service import MissionService

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
ALL = frozenset({SkillPermission.guild_member, SkillPermission.moderator})
TERMINAL = {MissionStatus.completed, MissionStatus.failed, MissionStatus.cancelled}


def _context(
    *,
    guild_id: int = 500,
    channel_id: int = 2002,
    user_id: int = 7,
    permissions: frozenset[SkillPermission] = ALL,
) -> SkillContext:
    return SkillContext(
        guild_id=guild_id,
        channel_id=channel_id,
        parent_id=None,
        user_id=user_id,
        permissions=permissions,
    )


async def _seed_threads(repo: Repository, *, guild_id: str = "500", count: int = 3) -> None:
    for index in range(count):
        thread_id = await repo.upsert_thread(
            discord_thread_id=f"{guild_id}-{index}",
            guild_id=guild_id,
            parent_channel_id="42",
            name=f"Login flow broken {index}",
        )
        await repo.insert_summary(
            thread_id=thread_id,
            requested_by="7",
            model="test-model",
            prompt_version="test-v1",
            status="unresolved",
            confidence=0.5,
            analysis_json=json.dumps(
                {"unanswered_questions": ["Why does login fail?"], "attempted_solutions": []}
            ),
            error_signatures=[],
        )


class _FailingKb(KbSearchSkill):
    async def execute(self, context, payload):
        raise RuntimeError("knowledge search exploded")


def _slow_pulse(started: asyncio.Event):
    class _SlowPulse(PulseSkill):
        async def execute(self, context, payload):
            started.set()
            await asyncio.sleep(10)
            return await super().execute(context, payload)

    return _SlowPulse


def _registry(repo: Repository, *, fail_kb: bool = False, pulse_cls=None) -> SkillRegistry:
    registry = SkillRegistry()
    pulse = pulse_cls or PulseSkill
    registry.register(pulse(repo, window_days=30, minimum_threads=3))
    registry.register(InsightsSkill(repo, window_days=30, minimum_threads=3))
    kb = _FailingKb(repo) if fail_kb else KbSearchSkill(repo)
    registry.register(kb)
    return registry


def _orchestrator(registry: SkillRegistry) -> SkillOrchestrator:
    return SkillOrchestrator(registry, allowed_guild_ids=[500], allowed_channel_ids=[])


def _service(tmp_path, repo: Repository, *, fail_kb: bool = False, pulse_cls=None, **overrides):
    settings = fake_settings(tmp_path, insights_min_threads=3, **overrides)
    service = MissionService(
        repo,
        _orchestrator(_registry(repo, fail_kb=fail_kb, pulse_cls=pulse_cls)),
        settings,
        clock=lambda: NOW,
    )
    return service, settings


async def _run_and_wait(service: MissionService, context: SkillContext, mission_id: int):
    updates = []
    done = asyncio.Event()

    async def on_update(view):
        updates.append(view)
        if view.status in TERMINAL:
            done.set()

    await service.start(context, mission_id, on_update=on_update)
    await asyncio.wait_for(done.wait(), timeout=10)
    return await service.get(guild_id=str(context.guild_id), mission_id=mission_id)


# --------------------------------------------------------------------- planner


def test_community_plan_is_ordered_and_deterministic(tmp_path):
    settings = fake_settings(tmp_path)
    available = {
        "pulse.community",
        "insights.community",
        "kb.search",
        "research.web",
        "knowledge.candidates",
    }

    plan = build_mission_plan(
        goal="  <|think|> Review login problems  ",
        mode=MissionMode.community,
        settings=settings,
        available_skills=available,
    )

    assert [step.skill_id for step in plan.steps] == [
        "pulse.community",
        "insights.community",
        "kb.search",
    ]
    assert [step.index for step in plan.steps] == [0, 1, 2]
    assert plan.steps[2].input["query"] == "Review login problems"
    assert plan.steps[2].input["limit"] == 5


def test_draft_prep_uses_research_only_when_registered(tmp_path):
    settings = fake_settings(tmp_path)

    with_research = build_mission_plan(
        goal="Prepare a login FAQ draft",
        mode=MissionMode.draft_prep,
        settings=settings,
        available_skills={"kb.search", "pulse.community", "research.web"},
    )
    without_research = build_mission_plan(
        goal="Prepare a login FAQ draft",
        mode=MissionMode.draft_prep,
        settings=settings,
        available_skills={"kb.search", "pulse.community"},
    )

    assert [step.skill_id for step in with_research.steps] == [
        "kb.search",
        "pulse.community",
        "research.web",
    ]
    assert [step.skill_id for step in without_research.steps] == [
        "kb.search",
        "pulse.community",
    ]


def test_plan_rejects_missing_capabilities_and_short_goals(tmp_path):
    settings = fake_settings(tmp_path)

    with pytest.raises(MissionPlanError):
        build_mission_plan(
            goal="Research redis",
            mode=MissionMode.research,
            settings=settings,
            available_skills={"kb.search"},
        )
    with pytest.raises(MissionPlanError):
        build_mission_plan(
            goal="ab",
            mode=MissionMode.community,
            settings=settings,
            available_skills={"pulse.community", "insights.community", "kb.search"},
        )


def test_plan_respects_the_configured_step_cap(tmp_path):
    settings = fake_settings(tmp_path, mission_max_steps=1)
    plan = build_mission_plan(
        goal="Review login problems",
        mode=MissionMode.community,
        settings=settings,
        available_skills={"pulse.community", "insights.community", "kb.search"},
    )
    assert [step.skill_id for step in plan.steps] == ["pulse.community"]


# --------------------------------------------------------------------- service


async def test_mission_completes_with_persisted_steps_and_trace(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)
    context = _context()

    view = await service.create_mission(
        context, goal="Review login problems in the community", mode=MissionMode.community
    )
    assert view.status is MissionStatus.planned
    assert view.trace_id
    assert view.completed_steps == 0

    final = await _run_and_wait(service, context, view.mission_id)

    assert final.status is MissionStatus.completed
    assert final.error_code is None
    assert final.completed_steps == 3
    assert final.progress_text() == "3/3 step(s)"
    assert [step.status for step in final.steps] == [StepStatus.succeeded] * 3
    assert "Status: completed" in final.summary
    assert "read-only mission" in final.summary.lower()
    assert final.started_at and final.finished_at

    row = await repo.get_mission(view.mission_id, guild_id="500")
    assert row is not None
    stored = json.loads(str(row["result_json"]))
    assert "Status: completed" in stored["summary"]
    assert (await repo.mission_counts(guild_id="500")) == {"completed": 1}

    actions = {
        str(item["action"]) for item in await repo.recent_audit(guild_id="500", limit=20)
    }
    assert {
        "mission.created",
        "mission.started",
        "mission.step_finished",
        "mission.completed",
    } <= actions


async def test_mission_is_idempotent_per_guild_key(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)
    context = _context()

    first = await service.create_mission(
        context,
        goal="Review login problems",
        mode=MissionMode.community,
        idempotency_key="slash:99",
    )
    second = await service.create_mission(
        context,
        goal="Review login problems",
        mode=MissionMode.community,
        idempotency_key="slash:99",
    )

    assert second.mission_id == first.mission_id
    assert (await repo.mission_counts(guild_id="500")) == {"planned": 1}


async def test_failed_step_skips_the_rest_and_reports_honest_progress(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo, fail_kb=True)
    context = _context()

    view = await service.create_mission(
        context, goal="Review login problems", mode=MissionMode.community
    )
    final = await _run_and_wait(service, context, view.mission_id)

    assert final.status is MissionStatus.failed
    assert final.error_code == "failed"
    assert [step.status for step in final.steps] == [
        StepStatus.succeeded,
        StepStatus.succeeded,
        StepStatus.failed,
    ]
    # Skipped steps did not produce a result and must not be counted as progress.
    assert final.completed_steps == 2
    assert final.progress_text() == "2/3 step(s)"
    assert "Status: failed" in final.summary
    assert "Failure:" in final.summary
    audits = {
        str(item["action"]) for item in await repo.recent_audit(guild_id="500", limit=20)
    }
    assert "mission.failed" in audits


async def test_cancellation_stops_the_in_flight_skill(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    started = asyncio.Event()
    service, _settings = _service(tmp_path, repo, pulse_cls=_slow_pulse(started))
    context = _context()

    view = await service.create_mission(
        context, goal="Review login problems", mode=MissionMode.community
    )
    await service.start(context, view.mission_id)
    await asyncio.wait_for(started.wait(), timeout=5)

    final = await service.cancel(context, view.mission_id)

    assert final.status is MissionStatus.cancelled
    assert final.cancel_requested is True
    assert final.completed_steps == 0
    assert final.progress_text() == "0/3 step(s)"
    assert all(
        step.status in {StepStatus.cancelled, StepStatus.succeeded} for step in final.steps
    )
    assert "Status: cancelled" in final.summary
    assert "Cancelled by a moderator." in final.summary
    assert await repo.mission_cancel_requested(view.mission_id, guild_id="500") is True


async def test_missions_are_invisible_across_guilds(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)

    view = await service.create_mission(
        _context(), goal="Review login problems", mode=MissionMode.community
    )

    with pytest.raises(MissionNotFoundError):
        await service.get(guild_id="999", mission_id=view.mission_id)
    with pytest.raises(MissionNotFoundError):
        await service.cancel(_context(guild_id=999), view.mission_id)
    assert await service.list(guild_id="999") == []


async def test_recovery_marks_interrupted_missions_failed(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)

    view = await service.create_mission(
        _context(), goal="Review login problems", mode=MissionMode.community
    )
    assert await repo.start_mission(
        view.mission_id, guild_id="500", started_at=NOW.isoformat()
    )

    assert await service.recover_interrupted() == 1

    final = await service.get(guild_id="500", mission_id=view.mission_id)
    assert final.status is MissionStatus.failed
    assert final.error_code == "interrupted"
    assert all(step.status is StepStatus.cancelled for step in final.steps)


async def test_control_view_reports_counts_activity_and_providers(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)
    context = _context()
    view = await service.create_mission(
        context, goal="Review login problems", mode=MissionMode.community
    )
    await _run_and_wait(service, context, view.mission_id)

    control = await service.control_view(
        guild_id="500", providers={"ollama": "reachable", "research": "disabled"}
    )

    assert control.guild_id == "500"
    assert control.counts.get("completed") == 1
    assert [item.mission_id for item in control.recent] == [view.mission_id]
    assert any(item.action == "mission.completed" for item in control.recent_activity)
    assert control.providers == {"ollama": "reachable", "research": "disabled"}


async def test_mission_creation_requires_moderator_and_enabled_mode(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)

    with pytest.raises(NotModeratorError):
        await service.create_mission(
            _context(permissions=frozenset({SkillPermission.guild_member})),
            goal="Review login problems",
            mode=MissionMode.community,
        )

    disabled, _settings = _service(tmp_path, repo, missions_enabled=False)
    with pytest.raises(MissionConflictError):
        await disabled.create_mission(
            _context(), goal="Review login problems", mode=MissionMode.community
        )
    with pytest.raises(MissionPlanError):
        await service.create_mission(
            _context(), goal="x" * 501, mode=MissionMode.community
        )


async def test_finished_mission_cannot_be_started_again(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    service, _settings = _service(tmp_path, repo)
    context = _context()
    view = await service.create_mission(
        context, goal="Review login problems", mode=MissionMode.community
    )
    await _run_and_wait(service, context, view.mission_id)

    with pytest.raises(MissionConflictError):
        await service.start(context, view.mission_id)
    # Cancelling a terminal mission is a no-op, not an error.
    cancelled = await service.cancel(context, view.mission_id)
    assert cancelled.status is MissionStatus.completed


async def test_shutdown_cancels_running_mission_tasks(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed_threads(repo)
    started = asyncio.Event()
    service, _settings = _service(tmp_path, repo, pulse_cls=_slow_pulse(started))
    context = _context()
    view = await service.create_mission(
        context, goal="Review login problems", mode=MissionMode.community
    )
    await service.start(context, view.mission_id)
    await asyncio.wait_for(started.wait(), timeout=5)

    await service.shutdown()
    await asyncio.sleep(0)

    final = await service.get(guild_id="500", mission_id=view.mission_id)
    assert final.status is MissionStatus.cancelled
