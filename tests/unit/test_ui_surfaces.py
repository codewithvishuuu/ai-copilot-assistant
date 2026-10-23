"""Bounded, explicit embeds for missions, Radar, research, planning and evaluation."""

from __future__ import annotations

from fakes import FIXED_TIME

from copilot.agents.contract import Reference
from copilot.core.chat_memory import MemoryStats
from copilot.core.community_pulse import PulseEvidence, PulseRecommendation
from copilot.core.content_planning import (
    CalendarEntryView,
    CalendarKind,
    CalendarState,
    ContextualReplyView,
    EventPlanView,
)
from copilot.core.evaluation import EvaluationCheck, EvaluationReport
from copilot.core.problem_radar import RadarGap, RadarProblem, RadarSnapshot
from copilot.missions.models import (
    MissionControlView,
    MissionMode,
    MissionStatus,
    MissionStepView,
    MissionView,
    StepStatus,
)
from copilot.research.provider import WebSource
from copilot.research.service import ResearchOutcome, ResearchStatus
from copilot.ui.embeds import build_memory_status_embed
from copilot.ui.missions import (
    calendar_embed,
    evaluation_embed,
    event_plan_embed,
    event_plan_list_embed,
    mission_control_embed,
    mission_embed,
    mission_list_embed,
    radar_embed,
    reply_draft_embed,
    reply_list_embed,
    research_embed,
)


def _step(index: int, status: StepStatus, **overrides) -> MissionStepView:
    values = {
        "step_id": index + 1,
        "index": index,
        "title": f"Step {index + 1}",
        "skill_id": "pulse.community",
        "status": status,
        "output_summary": "3 thread(s); 1 observation(s).",
        "references": [
            Reference(
                kind="thread",
                title="Login thread",
                url="https://discord.com/channels/500/1",
                snippet="evidence",
            )
        ],
    }
    values.update(overrides)
    return MissionStepView(**values)


def _mission(**overrides) -> MissionView:
    values = {
        "mission_id": 12,
        "guild_id": "500",
        "channel_id": "2002",
        "requester_id": "7",
        "goal": "Review login problems",
        "mode": MissionMode.community,
        "status": MissionStatus.completed,
        "trace_id": "abc123",
        "created_at": "2026-10-22T12:00:00+00:00",
        "updated_at": "2026-10-22T12:01:00+00:00",
        "started_at": "2026-10-22T12:00:01+00:00",
        "finished_at": "2026-10-22T12:01:00+00:00",
        "steps": [
            _step(0, StepStatus.succeeded),
            _step(1, StepStatus.succeeded, title="Step 2", skill_id="insights.community"),
        ],
        "references": [
            Reference(
                kind="thread",
                title="Login thread",
                url="https://discord.com/channels/500/1",
            )
        ],
        "summary": "Status: completed · 2/2 step(s)\nAll evidence is stored.",
    }
    values.update(overrides)
    return MissionView(**values)


def test_mission_embed_shows_steps_progress_and_references():
    embed = mission_embed(_mission())
    names = [field.name for field in embed.fields]
    assert "Status" in names
    values = {field.name: field.value for field in embed.fields}
    assert "completed" in values["Status"]
    assert "2/2 step(s)" in values["Status"]
    assert "abc123" in values["Status"]
    assert any("1. Step 1" in name for name in names)
    assert "Evidence references" in names
    assert "Read-only mission" in embed.footer.text


def test_failed_mission_embed_explains_the_safe_stop():
    embed = mission_embed(
        _mission(
            status=MissionStatus.failed,
            error_code="failed",
            steps=[
                _step(
                    0,
                    StepStatus.failed,
                    error="The skill failed safely.",
                    output_summary="",
                )
            ],
        )
    )
    values = {field.name: field.value for field in embed.fields}
    assert "Failure" in values
    assert "`failed`" in values["Failure"]
    step_field = next(field for field in embed.fields if field.name.startswith("1."))
    assert "The skill failed safely." in step_field.value


def test_empty_mission_list_and_control_states_are_explicit():
    empty = mission_list_embed([])
    assert "No missions yet" in empty.description

    control = mission_control_embed(
        MissionControlView(
            guild_id="500",
            counts={"completed": 1},
            recent=[_mission()],
            providers={"ollama": "reachable"},
        )
    )
    values = {field.name: field.value for field in control.fields}
    assert "1 mission(s)" in control.description
    assert values["Active"] == "No mission is currently planned or running."
    assert "ollama: reachable" in values["Providers"]
    assert "Recently finished" in values


def test_radar_embed_handles_insufficient_and_full_states():
    insufficient = radar_embed(
        RadarSnapshot(
            window_days=30,
            minimum_threads=5,
            total_threads=2,
            sufficient=False,
            coverage_summary="No Radar evidence yet.",
            notes=["Only stored summaries are read."],
        )
    )
    assert "Not enough summarized threads yet" in insufficient.description

    evidence = PulseEvidence(
        kind="thread",
        ref_id="500-1",
        title="Login thread",
        url="https://discord.com/channels/500/1",
    )
    recommendation = PulseRecommendation(
        recommendation_id="pulse-abc12345",
        kind="faq",
        title="FAQ: Login",
        rationale="Repeated question.",
        evidence=[evidence],
        suggested_action="Draft a FAQ.",
        expected_outcome="Estimate (not measured): fewer repeats.",
        uncertainty="medium",
    )
    snapshot = RadarSnapshot(
        window_days=30,
        minimum_threads=3,
        total_threads=6,
        sufficient=True,
        problems=[
            RadarProblem(
                problem_id="repeated-abc123",
                kind="repeated_question",
                title="How do I log in?",
                count=4,
                confidence="medium",
                evidence=[evidence],
                uncertainty="A shared wording does not prove a shared root cause.",
                kb_status="missing",
                suggested_action="Draft a FAQ.",
            )
        ],
        gaps=[
            RadarGap(
                gap_id="gap-abc12345",
                title="How do I log in?",
                count=4,
                confidence="medium",
                evidence=[evidence],
                note="No approved match.",
            )
        ],
        recommendations=[recommendation],
        coverage_summary="One topic has no approved match.",
        notes=["Evidence is stored only."],
    )
    embed = radar_embed(snapshot)
    names = [field.name for field in embed.fields]
    assert "How do I log in?" in names
    assert "Knowledge gap: How do I log in?" in names
    assert any("Recommendations" in name for name in names)
    assert "discord.com/channels/500/1" in embed.fields[0].value


def test_research_embeds_never_imply_sources_when_unavailable():
    unavailable = research_embed(
        ResearchOutcome(
            status=ResearchStatus.unavailable,
            query="redis timeouts",
            provider="none",
            reason="Web research is disabled.",
            report_id=3,
        )
    )
    assert "Web research unavailable" == unavailable.title
    assert "No search was performed" in unavailable.description
    assert unavailable.fields == []

    completed = research_embed(
        ResearchOutcome(
            status=ResearchStatus.completed,
            query="redis timeouts",
            provider="brave",
            summary="Saturation is the usual cause [1].",
            sources=(
                WebSource(
                    title="Redis latency",
                    url="https://example.com/redis",
                    snippet="latency",
                    provider="brave",
                ),
            ),
            synthesis="model",
            report_id=4,
        )
    )
    values = {field.name: field.value for field in completed.fields}
    assert "model synthesis" in values["Synthesis"]
    assert "[1] [Redis latency](https://example.com/redis)" in values["Sources (web search results)"]


def test_evaluation_embed_states_offline_scope_and_failures():
    report = EvaluationReport(
        guild_id="500",
        requested_by="7",
        created_at=FIXED_TIME.isoformat(),
        checks=[
            EvaluationCheck(
                check_id="skill.registry",
                category="contract",
                title="Skill registry contract",
                passed=True,
                detail="ok",
            ),
            EvaluationCheck(
                check_id="db.migrations",
                category="regression",
                title="Fresh database migrations",
                passed=False,
                detail="schema version too old",
            ),
        ],
        passed=False,
        passed_count=1,
        failed_count=1,
        summary="1/2 offline checks passed.",
    )
    embed = evaluation_embed(report)
    values = {field.name: field.value for field in embed.fields}
    assert "offline only" in values["Scope"]
    assert any("db.migrations" in name for name in values)
    assert "No model output quality" in embed.footer.text or "No model output quality" in embed.description


def test_planning_embeds_render_empty_and_linked_states():
    empty_calendar = calendar_embed([], include_cancelled=False)
    assert "No planned entries" in empty_calendar.description
    assert "never schedule" in empty_calendar.description

    plan = EventPlanView(
        plan_id=1,
        title="Workshop",
        brief="A debugging workshop.",
        scheduled_for="2026-11-05T18:00:00+00:00",
        target_channel_id="2002",
        agenda=["Opening"],
        checklist=["Confirm time"],
        roles=["Host"],
        state="draft",
        created_by="7",
        created_at="2026-10-22T12:00:00+00:00",
    )
    plan_embed = event_plan_embed(plan)
    assert "Planning template only" in plan_embed.footer.text
    assert any("Agenda" in field.name for field in plan_embed.fields)
    assert "No event plans yet" in event_plan_list_embed([]).description

    entry = CalendarEntryView(
        entry_id=1,
        title="Announce workshop",
        kind=CalendarKind.announcement,
        starts_at="2026-11-05T18:00:00+00:00",
        target_channel_id="2002",
        draft_id=4,
        event_plan_id=1,
        state=CalendarState.planned,
        created_by="7",
        created_at="2026-10-22T12:00:00+00:00",
    )
    calendar = calendar_embed([entry], include_cancelled=False)
    value = calendar.fields[0].value
    assert "Linked draft: `4`" in value
    assert "never posts or publishes" in calendar.footer.text

    reply = ContextualReplyView(
        reply_id=1,
        channel_id="2002",
        source_message_id="55",
        source_excerpt="A question.",
        tone="Concise",
        purpose="Help.",
        content="Try restarting @\u200beveryone-safe gateway.",
        state="draft",
        created_by="7",
        created_at="2026-10-22T12:00:00+00:00",
    )
    reply_embed = reply_draft_embed(reply)
    assert "Draft only" in reply_embed.footer.text
    assert "No open reply drafts" in reply_list_embed([]).description


def test_memory_status_embed_is_counts_only_and_states_its_source():
    embed = build_memory_status_embed(
        channel_id=2002,
        stats=MemoryStats(
            turns=4,
            oldest="2026-10-20T10:00:00+00:00",
            newest="2026-10-22T10:00:00+00:00",
            source="database",
        ),
        retention_days=7,
        max_stored_turns=40,
        context_turns=6,
        grounding_enabled=True,
    )
    rendered = str(embed.to_dict())
    values = {field.name: field.value for field in embed.fields}
    assert "4 turn(s)" in values["Stored turns"]
    assert "SQLite database" in values["Source"]
    assert "7 day(s)" in values["Bounds"] and "40" in values["Bounds"]
    assert "approved documents only" in values["KB grounding"]
    assert "never shown" in embed.description
    assert "secret" not in rendered.lower()
