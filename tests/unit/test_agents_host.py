"""Main Host Agent: planning, dependencies, bounded parallelism, failure and isolation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fakes import FakeProvider, fake_settings

from copilot.agents.builtin import (
    ContentBriefSkill,
    InsightsSkill,
    KbSearchSkill,
    KnowledgeCandidatesSkill,
    ModerationAssessSkill,
    PulseSkill,
    QualityChecksSkill,
    RadarSkill,
    ResearchSkill,
    SynthesisBriefSkill,
    TriageAnalyzeSkill,
    VisionAnalyzeSkill,
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
from copilot.agents.roles import RoleId
from copilot.core.evaluation import EvaluationService
from copilot.core.moderation_ai import ModerationService
from copilot.db.repository import Repository
from copilot.errors import (
    AgentRunConflictError,
    AgentRunNotFoundError,
    NotModeratorError,
)
from copilot.research.provider import WebSource
from copilot.research.service import ResearchOutcome, ResearchStatus

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


class _FakeResearch:
    def __init__(self, outcome: ResearchOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []

    async def research(self, *, guild_id: str, query: str, llm=None):
        self.calls.append((guild_id, query))
        return self.outcome


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


async def _wait_terminal(service, context, run_id, *, wait_s=10, images=()):
    updates = []
    done = asyncio.Event()

    async def on_update(view):
        updates.append(view)
        if view.status in TERMINAL:
            done.set()

    await service.start(context, run_id, images=images, on_update=on_update)
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
    skills = {
        "pulse.community",
        "insights.community",
        "radar.community",
        "kb.search",
        "synthesis.brief",
    }

    plan = planner.plan("how is the community doing", skills)

    assert [task.skill_id for task in plan.tasks] == [
        "pulse.community",
        "insights.community",
        "radar.community",
        "kb.search",
        "synthesis.brief",
    ]
    synthesis = plan.tasks[-1]
    assert synthesis.depends_on == (0, 1, 2, 3)
    assert synthesis.input["$upstream"] is True
    assert plan.unavailable == ()


def test_each_host_mode_orchestrates_its_specialist_roles(tmp_path):
    settings = fake_settings(tmp_path)
    planner = HostPlanner(settings)
    skills = {
        "pulse.community",
        "insights.community",
        "radar.community",
        "kb.search",
        "knowledge.candidates",
        "content.brief",
        "research.web",
        "triage.analyze",
        "moderation.assess",
        "quality.checks",
        "synthesis.brief",
    }
    expected = {
        HostMode.community: (
            "pulse.community",
            "insights.community",
            "radar.community",
            "kb.search",
            "synthesis.brief",
        ),
        HostMode.research: ("kb.search", "research.web", "synthesis.brief"),
        HostMode.draft_prep: (
            "kb.search",
            "pulse.community",
            "content.brief",
            "research.web",
            "synthesis.brief",
        ),
        HostMode.moderation: (
            "moderation.assess",
            "knowledge.candidates",
            "insights.community",
            "synthesis.brief",
        ),
        HostMode.triage: ("kb.search", "triage.analyze", "synthesis.brief"),
        HostMode.evaluation: ("quality.checks", "synthesis.brief"),
    }
    roles: set[RoleId] = set()
    for mode, expected_skills in expected.items():
        plan = planner.plan("community health and quality check", skills, mode=mode)
        assert tuple(task.skill_id for task in plan.tasks) == expected_skills
        roles.update(task.role_id for task in plan.tasks)

    assert roles == set(RoleId), f"roles not orchestrated: {set(RoleId) - roles}"


def test_one_step_budget_does_not_claim_synthesis_is_unregistered(tmp_path):
    settings = fake_settings(tmp_path, agent_max_steps=1)
    plan = HostPlanner(settings).plan(
        "how is the community doing",
        {"pulse.community", "insights.community", "kb.search", "synthesis.brief"},
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].skill_id == "pulse.community"
    notes = " ".join(plan.unavailable)
    assert "not registered" not in notes
    assert "capped at 1 step" in notes


def test_planner_adds_vision_only_when_an_image_is_supplied_and_registered(tmp_path):
    planner = HostPlanner(fake_settings(tmp_path))
    skills = {"kb.search", "triage.analyze", "vision.analyze", "synthesis.brief"}

    with_image = planner.plan(
        "debug this login screenshot", skills, mode=HostMode.triage, has_images=True
    )
    without_image = planner.plan(
        "debug this login traceback", skills, mode=HostMode.triage, has_images=False
    )

    assert [task.skill_id for task in with_image.tasks] == [
        "kb.search",
        "vision.analyze",
        "triage.analyze",
        "synthesis.brief",
    ]
    assert "vision.analyze" not in [task.skill_id for task in without_image.tasks]
    assert any("No screenshot was supplied" in note for note in without_image.unavailable)


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


async def test_specialist_modes_execute_end_to_end_through_the_host(tmp_db, tmp_path):
    await _seed_threads(Repository(tmp_db))
    repo = Repository(tmp_db)
    settings = fake_settings(tmp_path, insights_min_threads=3)
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "harmful": True,
                    "category": "harassment",
                    "severity": "high",
                    "confidence": 0.9,
                    "ambiguous": False,
                    "rationale": "Targeted insult.",
                    "recommended_next_step": "Human review.",
                }
            ),
            json.dumps(
                {
                    "content": "Draft suggestion for the community.",
                    "needs_research": False,
                    "research_notes": "",
                    "warnings": [],
                }
            ),
        ]
    )
    registry = _registry(repo)
    registry.register(KnowledgeCandidatesSkill(repo))
    registry.register(RadarSkill(repo, window_days=30, minimum_threads=3))
    registry.register(TriageAnalyzeSkill())
    registry.register(ModerationAssessSkill(ModerationService(repo, provider, settings)))
    registry.register(ContentBriefSkill(repo, provider, settings))
    orchestrator = _orchestrator(registry)
    evaluations = EvaluationService(repo, orchestrator, settings)
    registry.register(QualityChecksSkill(evaluations))
    service = AgentRunService(repo, orchestrator, settings, clock=lambda: NOW)
    context = _context()
    cases = [
        (
            HostMode.moderation,
            "you are worthless and should leave",
            [
                "moderation.assess",
                "knowledge.candidates",
                "insights.community",
                "synthesis.brief",
            ],
        ),
        (
            HostMode.triage,
            "Traceback: ConnectionRefusedError: connection refused",
            ["kb.search", "triage.analyze", "synthesis.brief"],
        ),
        (
            HostMode.draft_prep,
            "write a release announcement draft",
            ["kb.search", "pulse.community", "content.brief", "synthesis.brief"],
        ),
        (
            HostMode.evaluation,
            "run the offline quality evaluation",
            ["quality.checks", "synthesis.brief"],
        ),
    ]

    for mode, goal, expected_skills in cases:
        created = await service.create_run(context, goal=goal, mode=mode)
        final, _updates = await _wait_terminal(service, context, created.run_id)
        assert final.status is AgentRunStatus.completed, (mode, final.summary)
        assert [step.skill_id for step in final.steps] == expected_skills
        assert all(step.status is AgentStepStatus.succeeded for step in final.steps)

    assert await repo.list_moderation_incidents(guild_id="500") == []
    assert await repo.latest_agent_eval_run(guild_id="500") is None


def _research_service(tmp_db, tmp_path, outcome: ResearchOutcome):
    repo = Repository(tmp_db)
    fake = _FakeResearch(outcome)
    registry = SkillRegistry()
    registry.register(KbSearchSkill(repo))
    registry.register(ResearchSkill(fake))
    registry.register(SynthesisBriefSkill())
    settings = fake_settings(tmp_path)
    service = AgentRunService(
        repo, _orchestrator(registry), settings, clock=lambda: NOW
    )
    return fake, service


async def test_host_research_mode_executes_a_fake_provider_end_to_end(tmp_db, tmp_path):
    outcome = ResearchOutcome(
        status=ResearchStatus.completed,
        query="latest release",
        provider="fake-search",
        summary="Release notes are available in the results.",
        sources=(
            WebSource(
                title="Release notes",
                url="https://example.invalid/releases",
                snippet="Version 2.0 is available.",
                provider="fake-search",
            ),
        ),
    )
    fake, service = _research_service(tmp_db, tmp_path, outcome)
    context = _context()

    created = await service.create_run(
        context, goal="research the latest release", mode=HostMode.research
    )
    final, _updates = await _wait_terminal(service, context, created.run_id)

    assert final.status is AgentRunStatus.completed
    assert [step.skill_id for step in final.steps] == [
        "kb.search",
        "research.web",
        "synthesis.brief",
    ]
    research_step = final.steps[1]
    assert research_step.output is not None
    assert research_step.output["status"] == "completed"
    assert research_step.output["provider"] == "fake-search"
    assert research_step.output["citations"] == [
        "[1] Release notes — https://example.invalid/releases"
    ]
    assert research_step.references[0]["url"] == "https://example.invalid/releases"
    assert fake.calls == [("500", "research the latest release")]


@pytest.mark.parametrize(
    ("status", "error_code", "reason", "fragment"),
    [
        (ResearchStatus.failed, "provider_error", "provider timeout", "provider timeout"),
        (ResearchStatus.failed, "no_results", "no matching results", "no matching results"),
        (ResearchStatus.unavailable, None, "no search provider", "no search provider"),
    ],
)
async def test_host_research_reports_provider_errors_and_empty_results(
    tmp_db, tmp_path, status, error_code, reason, fragment
):
    outcome = ResearchOutcome(
        status=status,
        query="latest release",
        provider="fake-search",
        reason=reason,
        error_code=error_code,
    )
    fake, service = _research_service(tmp_db, tmp_path, outcome)
    context = _context()

    created = await service.create_run(
        context, goal="research the latest release", mode=HostMode.research
    )
    final, _updates = await _wait_terminal(service, context, created.run_id)

    assert final.status is AgentRunStatus.partial
    research_step = final.steps[1]
    assert research_step.status is AgentStepStatus.failed
    assert fragment in (research_step.error or "")
    assert final.steps[2].status is AgentStepStatus.skipped
    assert fake.calls == [("500", "research the latest release")]


async def _vision_service(tmp_db, tmp_path, provider):
    repo = Repository(tmp_db)
    settings = fake_settings(tmp_path)
    registry = SkillRegistry()
    registry.register(KbSearchSkill(repo))
    registry.register(TriageAnalyzeSkill())
    registry.register(VisionAnalyzeSkill(provider, settings))
    registry.register(SynthesisBriefSkill())
    service = AgentRunService(
        repo, _orchestrator(registry), settings, clock=lambda: NOW
    )
    return repo, service


async def test_host_triage_sends_images_only_to_the_verified_vision_skill(tmp_db, tmp_path):
    image = b"HOST_IMAGE_MARKER_123"
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "summary": "A login screen shows a 500 error.",
                    "observations": ["The server error banner is visible."],
                    "likely_causes": ["The login service failed."],
                    "safe_steps": ["Check the login service logs."],
                    "uncertainty": ["The traceback is not visible."],
                }
            )
        ],
        vision=True,
    )
    repo, service = await _vision_service(tmp_db, tmp_path, provider)
    context = _context()

    created = await service.create_run(
        context,
        goal="debug this login screenshot",
        mode=HostMode.triage,
        has_images=True,
    )
    assert [step.skill_id for step in created.steps] == [
        "kb.search",
        "vision.analyze",
        "triage.analyze",
        "synthesis.brief",
    ]

    final, _updates = await _wait_terminal(
        service, context, created.run_id, images=(image,)
    )

    assert final.status is AgentRunStatus.completed
    vision_step = next(step for step in final.steps if step.skill_id == "vision.analyze")
    assert vision_step.output is not None
    assert vision_step.output["vision_used"] is True
    assert vision_step.output["observations"] == ["The server error banner is visible."]
    assert provider.calls[0]["images"] == [image]

    run_row = await repo.get_agent_run(created.run_id, guild_id="500")
    step_rows = await repo.list_agent_run_steps(created.run_id, guild_id="500")
    stored = " ".join(
        str(value or "")
        for value in [run_row["plan_json"], run_row["result_json"]]
        + [
            value
            for row in step_rows
            for value in (row["input_json"], row["output_json"], row["references_json"])
        ]
    )
    assert "HOST_IMAGE_MARKER_123" not in stored


async def test_host_triage_reports_unavailable_vision_without_sending_images(
    tmp_db, tmp_path
):
    image = b"HOST_IMAGE_MARKER_456"
    provider = FakeProvider(vision=False)
    _repo, service = await _vision_service(tmp_db, tmp_path, provider)
    context = _context()

    created = await service.create_run(
        context,
        goal="debug this login screenshot",
        mode=HostMode.triage,
        has_images=True,
    )
    final, _updates = await _wait_terminal(
        service, context, created.run_id, images=(image,)
    )

    assert final.status is AgentRunStatus.completed
    vision_step = next(step for step in final.steps if step.skill_id == "vision.analyze")
    assert vision_step.output is not None
    assert vision_step.output["vision_used"] is False
    assert vision_step.output["vision_status"] == "unavailable"
    assert vision_step.output["observations"] == []
    assert provider.calls == []


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
