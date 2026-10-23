"""Main Host Agent: planning, dependencies, bounded parallelism, failure and isolation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fakes import fake_settings

from copilot.agents.builtin import (
    InsightsSkill,
    KbSearchSkill,
    PulseSkill,
    SynthesisBriefSkill,
)
from copilot.agents.contract import SkillContext, SkillPermission
from copilot.agents.host import (
    AgentRunService,
    AgentRunStatus,
    AgentStepStatus,
    HostMode,
    HostPlanner,
    classify_goal,
)
from copilot.agents.orchestrator import SkillOrchestrator
from copilot.agents.registry import SkillRegistry
from copilot.db.repository import Repository
from copilot.errors import (
    AgentRunConflictError,
    AgentRunNotFoundError,
    NotModeratorError,
)

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
ALL = frozenset({SkillPermission.guild_member, SkillPermission.moderator})
TERMINAL = {
    AgentRunStatus.completed,
    AgentRunStatus.partial,
    AgentRunStatus.failed,
    AgentRunStatus.cancelled,
}


def _context(*, guild_id: int = 500, user_id: int = 7) -> SkillContext:
    return SkillContext(
        guild_id=guild_id,
        channel_id=2002,
        parent_id=None,
        user_id=user_id,
        permissions=ALL,
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


def _registry(repo: Repository, *, fail_kb: bool = False, pulse_cls=None, insights_cls=None):
    registry = SkillRegistry()
    registry.register((pulse_cls or PulseSkill)(repo, window_days=30, minimum_threads=3))
    registry.register(
        (insights_cls or InsightsSkill)(repo, window_days=30, minimum_threads=3)
    )
    registry.register(_FailingKb(repo) if fail_kb else KbSearchSkill(repo))
    registry.register(SynthesisBriefSkill())
    return registry


def _orchestrator(registry: SkillRegistry, *, guild_ids=(500,)) -> SkillOrchestrator:
    return SkillOrchestrator(registry, allowed_guild_ids=list(guild_ids), allowed_channel_ids=[])


def _service(tmp_path, repo, registry=None, *, guild_ids=(500,), **overrides):
    settings = fake_settings(tmp_path, insights_min_threads=3, **overrides)
    registry = registry or _registry(repo)
    service = AgentRunService(
        repo,
        _orchestrator(registry, guild_ids=guild_ids),
        settings,
        clock=lambda: NOW,
    )
    return service


async def _wait_terminal(service, context, run_id, *, wait_s=10):
    updates = []
    done = asyncio.Event()

    async def on_update(view):
        updates.append(view)
        if view.status in TERMINAL:
            done.set()

    await service.start(context, run_id, on_update=on_update)
    await asyncio.wait_for(done.wait(), timeout=wait_s)
    return await service.get(guild_id=str(context.guild_id), run_id=run_id), updates


# ------------------------------------------------------------------- planning


def test_classify_goal_selects_bounded_modes():
    assert classify_goal("latest release research with sources") is HostMode.research
    assert classify_goal("write an announcement draft") is HostMode.draft_prep
    assert classify_goal("abuse and threat report") is HostMode.moderation
    assert classify_goal("debug this traceback in the log") is HostMode.triage
    assert classify_goal("how is the community doing") is HostMode.community


def test_planner_builds_dependency_graph_with_synthesis_last(tmp_path):
    settings = fake_settings(tmp_path)
    planner = HostPlanner(settings)
    skills = {"pulse.community", "insights.community", "kb.search", "synthesis.brief"}

    plan = planner.plan("how is the community doing", skills)

    assert [task.skill_id for task in plan.tasks] == [
        "pulse.community",
        "insights.community",
        "kb.search",
        "synthesis.brief",
    ]
    synthesis = plan.tasks[-1]
    assert synthesis.depends_on == (0, 1, 2)
    assert synthesis.input["$upstream"] is True
    assert plan.unavailable == ()


def test_planner_reports_unavailable_web_research_honestly(tmp_path):
    settings = fake_settings(tmp_path)
    planner = HostPlanner(settings)

    plan = planner.plan("research the latest release", {"kb.search", "synthesis.brief"})

    assert plan.mode is HostMode.research
    assert all(task.skill_id != "research.web" for task in plan.tasks)
    assert any("Web Researcher is unavailable" in note for note in plan.unavailable)


# ------------------------------------------------------------------ execution


async def test_run_executes_dependencies_and_passes_upstream_outputs(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)
    context = _context()

    created = await service.create_run(context, goal="how is the community doing")
    final, _updates = await _wait_terminal(service, context, created.run_id)

    assert final.status is AgentRunStatus.completed
    assert [step.status for step in final.steps] == [AgentStepStatus.succeeded] * 4
    synthesis = final.steps[-1]
    assert synthesis.depends_on == (0, 1, 2)
    assert synthesis.output is not None
    assert synthesis.output["sources_used"] == 3
    assert "read-only evidence combination" in synthesis.output["brief"]


async def test_independent_steps_run_in_parallel_under_the_semaphore(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    pulse_started = asyncio.Event()
    insights_started = asyncio.Event()

    class WaitPulse(PulseSkill):
        async def execute(self, context, payload):
            pulse_started.set()
            await asyncio.wait_for(insights_started.wait(), timeout=2)
            return await super().execute(context, payload)

    class WaitInsights(InsightsSkill):
        async def execute(self, context, payload):
            insights_started.set()
            await asyncio.wait_for(pulse_started.wait(), timeout=2)
            return await super().execute(context, payload)

    service = _service(
        tmp_path,
        repo,
        _registry(repo, pulse_cls=WaitPulse, insights_cls=WaitInsights),
        agent_max_parallel=2,
    )
    context = _context()

    created = await service.create_run(context, goal="how is the community doing")
    final, _updates = await _wait_terminal(service, context, created.run_id)

    assert final.status is AgentRunStatus.completed
    assert final.steps[0].output is not None
    assert final.steps[1].output is not None


async def test_partial_failure_skips_dependents_and_is_reported(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo, _registry(repo, fail_kb=True))
    context = _context()

    created = await service.create_run(context, goal="how is the community doing")
    final, _updates = await _wait_terminal(service, context, created.run_id)

    assert final.status is AgentRunStatus.partial
    assert final.steps[2].status is AgentStepStatus.failed
    assert final.steps[3].status is AgentStepStatus.skipped
    assert final.error_code == "partial_failure"
    assert "FAILED" in final.summary


async def test_cancellation_stops_a_running_run_and_persists_it(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    started = asyncio.Event()

    class SlowPulse(PulseSkill):
        async def execute(self, context, payload):
            started.set()
            await asyncio.sleep(30)
            return await super().execute(context, payload)

    service = _service(tmp_path, repo, _registry(repo, pulse_cls=SlowPulse))
    context = _context()
    created = await service.create_run(context, goal="how is the community doing")

    await service.start(context, created.run_id)
    await asyncio.wait_for(started.wait(), timeout=5)
    cancelled = await service.cancel(context, created.run_id)

    assert cancelled.status is AgentRunStatus.cancelled
    assert cancelled.error_code == "cancelled"
    assert cancelled.steps[0].status is AgentStepStatus.cancelled
    assert all(step.status is not AgentStepStatus.running for step in cancelled.steps)


async def test_idempotency_key_reuses_the_existing_run(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)
    context = _context()

    first = await service.create_run(context, goal="how is the community doing", idempotency_key="k1")
    second = await service.create_run(context, goal="different goal text", idempotency_key="k1")

    assert second.run_id == first.run_id
    assert second.goal == first.goal


async def test_agent_disabled_refuses_new_runs(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo, agent_enabled=False)

    with pytest.raises(AgentRunConflictError):
        await service.create_run(_context(), goal="how is the community doing")


# ------------------------------------------------------------------ isolation


async def test_runs_are_guild_scoped_for_reads_and_cancel(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)
    created = await service.create_run(_context(guild_id=500), goal="how is the community doing")

    with pytest.raises(AgentRunNotFoundError):
        await service.get(guild_id="999", run_id=created.run_id)
    with pytest.raises(AgentRunNotFoundError):
        await service.cancel(_context(guild_id=999), created.run_id)


async def test_non_moderator_cannot_create_or_cancel(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)
    member = SkillContext(
        guild_id=500,
        channel_id=2002,
        parent_id=None,
        user_id=8,
        permissions=frozenset({SkillPermission.guild_member}),
    )

    with pytest.raises(NotModeratorError):
        await service.create_run(member, goal="how is the community doing")

    mod_ctx = _context()
    created = await service.create_run(mod_ctx, goal="how is the community doing")
    with pytest.raises(NotModeratorError):
        await service.cancel(member, created.run_id)


async def test_guild_outside_allowlist_fails_steps_safely(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo, guild_ids=(500,))
    context = _context(guild_id=999)
    created = await service.create_run(context, goal="how is the community doing")

    final, _updates = await _wait_terminal(service, context, created.run_id)

    assert final.status is AgentRunStatus.failed
    assert final.steps[0].status is AgentStepStatus.failed
    assert final.steps[3].status is AgentStepStatus.skipped
    assert final.steps[0].output is None
