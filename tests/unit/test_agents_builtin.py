"""Phase 1 built-in adapters: guild/approval isolation and no public actions."""

from __future__ import annotations

import json

from fakes import FakeProvider, fake_settings

from copilot.agents.builtin import QualityChecksSkill, build_default_registry
from copilot.agents.contract import SkillContext, SkillPermission, SkillStatus, TaskRequest
from copilot.agents.orchestrator import SkillOrchestrator
from copilot.agents.roles import RoleRegistry
from copilot.bot import CommunityCopilot
from copilot.core.approvals import (
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
)
from copilot.core.evaluation import EvaluationService
from copilot.core.knowledge_candidates import propose_candidate
from copilot.core.moderation_ai import ModerationService
from copilot.db.repository import Repository


def context(*permissions: SkillPermission) -> SkillContext:
    return SkillContext(
        guild_id=500,
        channel_id=2002,
        parent_id=None,
        user_id=7,
        permissions=frozenset(permissions or (SkillPermission.guild_member,)),
    )


def orchestrator(repo: Repository) -> SkillOrchestrator:
    registry = build_default_registry(
        repo,
        insights_window_days=30,
        insights_minimum_threads=3,
    )
    return SkillOrchestrator(
        registry,
        allowed_guild_ids=[500],
        allowed_channel_ids=[],
    )


async def _seed_summary(
    repo: Repository,
    *,
    guild_id: str,
    index: int,
    status: str = "unresolved",
    errors: list[str] | None = None,
    analysis: dict | None = None,
) -> None:
    thread_id = await repo.upsert_thread(
        discord_thread_id=f"{guild_id}-{index}",
        guild_id=guild_id,
        parent_channel_id=None,
        name=f"thread {index}",
    )
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="test-model",
        prompt_version="test-v1",
        status=status,
        confidence=0.5,
        analysis_json=json.dumps(analysis or {}),
        error_signatures=errors or [],
    )


async def test_kb_search_adapter_is_guild_scoped_and_approved_only(tmp_db):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Guild A approved fix",
        body="deploy worker restart",
        url=None,
        approved=True,
    )
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=2,
        guild_id="500",
        title="Guild A unreviewed fingerprint",
        body="deploy worker restart",
        url=None,
        approved=False,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=3,
        guild_id="999",
        title="Guild B approved fix",
        body="deploy worker restart",
        url=None,
        approved=True,
    )

    result = await orchestrator(repo).run(
        TaskRequest(skill_id="kb.search", input={"query": "deploy worker"}),
        context(SkillPermission.guild_member),
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    titles = [reference["title"] for reference in result.output["references"]]
    assert titles == ["Guild A approved fix"]
    assert [reference.title for reference in result.references] == titles


async def test_insights_adapter_uses_only_the_callers_guild(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed_summary(repo, guild_id="500", index=index, errors=["boom"])
    for index in range(3):
        await _seed_summary(repo, guild_id="999", index=index, status="resolved", errors=["other"])

    result = await orchestrator(repo).run(
        TaskRequest(skill_id="insights.community"), context(SkillPermission.moderator)
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    assert result.output["total_threads"] == 3
    assert result.output["status_counts"] == {"unresolved": 3}
    assert result.output["top_errors"] == [["boom", 3]]


async def test_insights_adapter_requires_moderator_permission(tmp_db):
    result = await orchestrator(Repository(tmp_db)).run(
        TaskRequest(skill_id="insights.community"), context(SkillPermission.guild_member)
    )

    assert result.status is SkillStatus.denied
    assert result.output is None


async def test_default_registry_contains_only_read_only_capabilities(tmp_db):
    registry = build_default_registry(Repository(tmp_db), insights_window_days=30, insights_minimum_threads=3)

    assert registry.ids == (
        "insights.community",
        "kb.search",
        "knowledge.candidates",
        "pulse.community",
        "radar.community",
        "synthesis.brief",
        "triage.analyze",
    )
    assert all(skill.read_only is True for skill in registry.all())
    assert not any("publish" in skill_id or "delete" in skill_id for skill_id in registry.ids)


async def test_pulse_skill_is_moderator_only_and_evidence_backed(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed_summary(repo, guild_id="500", index=index, errors=["ECONNREFUSED redis"])

    denied = await orchestrator(repo).run(
        TaskRequest(skill_id="pulse.community"), context(SkillPermission.guild_member)
    )
    allowed = await orchestrator(repo).run(
        TaskRequest(skill_id="pulse.community"), context(SkillPermission.moderator)
    )

    assert denied.status is SkillStatus.denied
    assert allowed.status is SkillStatus.ok
    assert allowed.output is not None
    pulse = allowed.output["pulse"]
    assert pulse["sufficient"] is True
    assert pulse["total_threads"] == 3
    assert pulse["recommendations"]
    assert all(item["evidence"] for item in pulse["recommendations"])
    assert allowed.references


async def test_candidate_list_skill_is_moderator_only_and_reads_pending_only(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await repo.upsert_thread(
        discord_thread_id="500-1", guild_id="500", parent_channel_id="42", name="thread 1"
    )
    await propose_candidate(
        ApprovalActor(
            actor_id="7",
            guild_id=500,
            kind=ActorKind.human,
            permissions=frozenset({ApprovalPermission.propose}),
        ),
        repo,
        thread_row_id=thread_id,
        body="Restart the gateway.",
    )

    denied = await orchestrator(repo).run(
        TaskRequest(skill_id="knowledge.candidates"), context(SkillPermission.guild_member)
    )
    allowed = await orchestrator(repo).run(
        TaskRequest(skill_id="knowledge.candidates", input={"limit": 5}),
        context(SkillPermission.moderator),
    )

    assert denied.status is SkillStatus.denied
    assert allowed.status is SkillStatus.ok
    assert allowed.output is not None
    assert len(allowed.output["candidates"]) == 1
    assert allowed.output["candidates"][0]["state"] == "proposed"
    assert "body" not in allowed.output["candidates"][0]


async def test_radar_skill_returns_evidence_backed_problems(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed_summary(repo, guild_id="500", index=index, errors=["ECONNREFUSED redis"])

    result = await orchestrator(repo).run(
        TaskRequest(skill_id="radar.community"), context(SkillPermission.moderator)
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    snapshot = result.output["snapshot"]
    assert snapshot["total_threads"] == 3
    assert snapshot["problems"], "a recurring error should appear as a radar problem"
    assert result.references, "radar entries must keep reproducible evidence"
    assert all(item.kind for item in result.references)


async def test_triage_skill_separates_signals_from_uncertainty_and_redacts_secrets(tmp_db):
    result = await orchestrator(Repository(tmp_db)).run(
        TaskRequest(
            skill_id="triage.analyze",
            input={
                "content": (
                    "Traceback (most recent call last):\n"
                    "ConnectionRefusedError: [Errno 111] Connection refused\n"
                    "ERROR token=supersecretvalue"
                )
            },
        ),
        context(SkillPermission.guild_member),
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    assert result.output["error_line_count"] >= 1
    assert any("service" in item for item in result.output["inferences"])
    assert any("full traceback" in item for item in result.output["safe_steps"])
    assert any("not a verified diagnosis" in item for item in [result.output["summary"]])
    assert "supersecretvalue" not in json.dumps(result.output)


async def test_vision_skill_uses_verified_capability_and_labels_observations(tmp_db, tmp_path):
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "summary": "The login form shows a server error.",
                    "observations": ["A red 500 banner is visible above the login button."],
                    "likely_causes": ["The login service returned an internal error."],
                    "safe_steps": ["Check the service logs for the same timestamp."],
                    "uncertainty": ["The screenshot does not show the traceback."],
                }
            )
        ],
        vision=True,
    )
    registry = build_default_registry(
        Repository(tmp_db),
        insights_window_days=30,
        insights_minimum_threads=3,
        provider=provider,
        settings=fake_settings(tmp_path),
    )
    runner = SkillOrchestrator(registry, allowed_guild_ids=[500], allowed_channel_ids=[])

    result = await runner.run(
        TaskRequest(
            skill_id="vision.analyze",
            input={"goal": "why does login fail", "images": [b"sanitized-image-bytes"]},
        ),
        context(SkillPermission.guild_member),
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    assert result.output["vision_used"] is True
    assert result.output["vision_status"] == "used"
    assert result.output["observations"] == ["A red 500 banner is visible above the login button."]
    assert provider.calls[0]["images"] == [b"sanitized-image-bytes"]
    assert "untrusted screenshots" in provider.calls[0]["user"]


async def test_vision_skill_never_sends_images_without_model_vision(tmp_db, tmp_path):
    provider = FakeProvider(vision=False)
    registry = build_default_registry(
        Repository(tmp_db),
        insights_window_days=30,
        insights_minimum_threads=3,
        provider=provider,
        settings=fake_settings(tmp_path),
    )
    runner = SkillOrchestrator(registry, allowed_guild_ids=[500], allowed_channel_ids=[])

    result = await runner.run(
        TaskRequest(
            skill_id="vision.analyze",
            input={"goal": "why does login fail", "images": [b"sanitized-image-bytes"]},
        ),
        context(SkillPermission.guild_member),
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    assert result.output["vision_used"] is False
    assert result.output["vision_status"] == "unavailable"
    assert result.output["observations"] == []
    assert provider.calls == []


async def test_vision_skill_honours_configuration_and_provider_errors(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    disabled_provider = FakeProvider(vision=True)
    disabled_registry = build_default_registry(
        repo,
        insights_window_days=30,
        insights_minimum_threads=3,
        provider=disabled_provider,
        settings=fake_settings(tmp_path, vision_enabled=False),
    )
    disabled = await SkillOrchestrator(
        disabled_registry, allowed_guild_ids=[500], allowed_channel_ids=[]
    ).run(
        TaskRequest(
            skill_id="vision.analyze",
            input={"goal": "why does login fail", "images": [b"sanitized-image-bytes"]},
        ),
        context(SkillPermission.guild_member),
    )

    assert disabled.output is not None
    assert disabled.output["vision_status"] == "disabled"
    assert disabled.output["observations"] == []
    assert disabled_provider.calls == []

    failing_provider = FakeProvider(responses=[RuntimeError("provider offline")], vision=True)
    failing_registry = build_default_registry(
        repo,
        insights_window_days=30,
        insights_minimum_threads=3,
        provider=failing_provider,
        settings=fake_settings(tmp_path),
    )
    failed = await SkillOrchestrator(
        failing_registry, allowed_guild_ids=[500], allowed_channel_ids=[]
    ).run(
        TaskRequest(
            skill_id="vision.analyze",
            input={"goal": "why does login fail", "images": [b"sanitized-image-bytes"]},
        ),
        context(SkillPermission.guild_member),
    )

    assert failed.status is SkillStatus.ok
    assert failed.output is not None
    assert failed.output["vision_status"] == "error"
    assert failed.output["observations"] == []
    assert "no visual conclusions" in failed.output["summary"]


async def test_moderation_assess_skill_is_classification_only(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "harmful": True,
                    "category": "harassment",
                    "severity": "high",
                    "confidence": 0.91,
                    "ambiguous": False,
                    "rationale": "Targeted insult.",
                    "recommended_next_step": "Human moderator review.",
                }
            )
        ]
    )
    service = ModerationService(repo, provider, fake_settings(tmp_path))
    registry = build_default_registry(
        repo,
        insights_window_days=30,
        insights_minimum_threads=3,
        moderation_service=service,
    )
    runner = SkillOrchestrator(registry, allowed_guild_ids=[500], allowed_channel_ids=[])

    denied = await runner.run(
        TaskRequest(skill_id="moderation.assess", input={"content": "you are worthless"}),
        context(SkillPermission.guild_member),
    )
    allowed = await runner.run(
        TaskRequest(skill_id="moderation.assess", input={"content": "you are worthless"}),
        context(SkillPermission.moderator),
    )

    assert denied.status is SkillStatus.denied
    assert allowed.status is SkillStatus.ok
    assert allowed.output is not None
    assert allowed.output["harmful"] is True
    assert allowed.output["category"] == "harassment"
    assert "No moderation action was taken" in allowed.output["summary"]
    assert await repo.list_moderation_incidents(guild_id="500") == []
    assert await repo.recent_audit(guild_id="500") == []


async def test_content_brief_skill_never_persists_or_publishes(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Release process",
        body="Announce the release date after the final smoke test.",
        url=None,
        approved=True,
    )
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "content": "We are shipping the release after a final smoke test.",
                    "needs_research": False,
                    "research_notes": "",
                    "warnings": [],
                }
            )
        ]
    )
    registry = build_default_registry(
        repo,
        insights_window_days=30,
        insights_minimum_threads=3,
        provider=provider,
        settings=fake_settings(tmp_path),
    )
    runner = SkillOrchestrator(registry, allowed_guild_ids=[500], allowed_channel_ids=[])

    result = await runner.run(
        TaskRequest(
            skill_id="content.brief",
            input={"topic": "release announcement for the community"},
        ),
        context(SkillPermission.moderator),
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    assert "smoke test" in result.output["draft"]
    assert result.output["sources_used"] == 1
    assert "no Content Studio draft was saved" in result.output["summary"]
    assert [ref["title"] for ref in result.output["references"]] == ["Release process"]
    assert await repo.recent_audit(guild_id="500") == []


async def test_quality_checks_skill_runs_offline_checks_without_persisting(tmp_db, tmp_path):
    repo = Repository(tmp_db)
    runner = orchestrator(repo)
    evaluations = EvaluationService(repo, runner, fake_settings(tmp_path))
    runner.registry.register(QualityChecksSkill(evaluations))

    result = await runner.run(
        TaskRequest(skill_id="quality.checks"), context(SkillPermission.moderator)
    )

    assert result.status is SkillStatus.ok
    assert result.output is not None
    assert result.output["offline_only"] is True
    assert result.output["model_quality_evaluated"] is False
    assert result.output["passed"] is True
    assert await repo.latest_agent_eval_run(guild_id="500") is None


async def test_every_specialist_role_has_a_registered_bot_skill(tmp_path):
    bot = CommunityCopilot(
        fake_settings(
            tmp_path,
            web_research_enabled=True,
            web_search_provider="brave",
            brave_search_api_key="unit-test-key",
        )
    )
    try:
        available = RoleRegistry().available(set(bot.skill_registry.ids))
        assert all(available.values()), available
        assert len(available) == 8
    finally:
        await bot.provider.aclose()


async def test_bot_wires_the_read_only_default_registry(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        # No search provider/key is configured, so the unavailable research skill
        # is not advertised to the model-controlled registry.
        assert bot.skill_registry.ids == (
            "content.brief",
            "insights.community",
            "kb.search",
            "knowledge.candidates",
            "moderation.assess",
            "pulse.community",
            "quality.checks",
            "radar.community",
            "synthesis.brief",
            "triage.analyze",
            "vision.analyze",
        )
        assert all(skill.read_only is True for skill in bot.skill_registry.all())
        assert bot.orchestrator.registry is bot.skill_registry
    finally:
        await bot.provider.aclose()


async def test_bot_registers_research_only_when_a_provider_is_configured(tmp_path):
    bot = CommunityCopilot(
        fake_settings(
            tmp_path,
            web_research_enabled=True,
            web_search_provider="brave",
            brave_search_api_key="unit-test-key",
        )
    )
    try:
        assert "research.web" in bot.skill_registry.ids
    finally:
        await bot.provider.aclose()
