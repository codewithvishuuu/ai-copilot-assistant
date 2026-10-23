"""Phase 1 built-in adapters: guild/approval isolation and no public actions."""

from __future__ import annotations

import json

from fakes import fake_settings

from copilot.agents.builtin import build_default_registry
from copilot.agents.contract import SkillContext, SkillPermission, SkillStatus, TaskRequest
from copilot.agents.orchestrator import SkillOrchestrator
from copilot.bot import CommunityCopilot
from copilot.core.approvals import (
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
)
from copilot.core.knowledge_candidates import propose_candidate
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
        "synthesis.brief",
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


async def test_bot_wires_the_read_only_default_registry(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        assert bot.skill_registry.ids == (
            "insights.community",
            "kb.search",
            "knowledge.candidates",
            "pulse.community",
            "research.web",
            "synthesis.brief",
        )
        assert all(skill.read_only is True for skill in bot.skill_registry.all())
        assert bot.orchestrator.registry is bot.skill_registry
    finally:
        await bot.provider.aclose()
