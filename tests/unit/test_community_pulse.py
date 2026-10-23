"""Community Pulse: deterministic, evidence-backed, aggregate-only analysis."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fakes import fake_settings

from copilot.core.approvals import (
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
    ApprovalService,
)
from copilot.core.community_pulse import (
    compute_community_pulse,
    create_draft_from_recommendation,
)
from copilot.core.content_studio import ContentStudio, ContentTarget, DraftStatus
from copilot.db.repository import Repository
from copilot.errors import PulseRecommendationNotFoundError

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
ALL = frozenset(
    {
        ApprovalPermission.propose,
        ApprovalPermission.decide,
        ApprovalPermission.cancel,
        ApprovalPermission.execute,
    }
)


async def _seed(
    repo: Repository,
    *,
    guild_id: str = "500",
    index: int,
    name: str | None = None,
    status: str = "unresolved",
    questions: list[str] | None = None,
    errors: list[str] | None = None,
) -> int:
    thread_id = await repo.upsert_thread(
        discord_thread_id=f"{guild_id}-{index}",
        guild_id=guild_id,
        parent_channel_id="42",
        name=name or f"thread {index}",
    )
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="test-model",
        prompt_version="test-v1",
        status=status,
        confidence=0.5,
        analysis_json=json.dumps(
            {
                "unanswered_questions": questions or [],
                "attempted_solutions": [{"text": "restart the gateway", "source_message_ids": []}],
            }
        ),
        error_signatures=errors or [],
    )
    return thread_id


async def _pulse(repo, **overrides):
    values = {"guild_id": "500", "window_days": 30, "minimum_threads": 3, "clock": lambda: NOW}
    values.update(overrides)
    return await compute_community_pulse(repo, **values)


def _actor() -> ApprovalActor:
    return ApprovalActor(
        actor_id="7", guild_id=500, kind=ActorKind.human, permissions=ALL
    )


async def test_minimum_data_threshold_returns_no_observations_or_recommendations(tmp_db):
    repo = Repository(tmp_db)
    await _seed(repo, index=1, questions=["How do I reset a password?"])
    await _seed(repo, index=2, questions=["How do I reset a password?"])

    pulse = await _pulse(repo, minimum_threads=3)

    assert pulse.sufficient is False
    assert pulse.total_threads == 2
    assert pulse.observations == []
    assert pulse.recommendations == []
    assert pulse.minimum_threads == 3


async def test_threshold_boundary_counts_distinct_threads(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed(repo, index=index, questions=["How do I reset a password?"])

    pulse = await _pulse(repo, minimum_threads=3)

    assert pulse.sufficient is True
    assert pulse.total_threads == 3


async def test_repeated_question_is_evidence_backed_and_reproducible(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed(
            repo,
            index=index,
            name=f"Password reset {index}",
            questions=["How do I reset my password?"],
        )

    first = await _pulse(repo)
    second = await _pulse(repo)

    assert first.sufficient and second.sufficient
    repeated = [item for item in first.observations if item.kind == "repeated_question"]
    assert repeated and repeated[0].count == 3
    assert len(repeated[0].evidence) == 3
    assert all(item.kind == "thread" for item in repeated[0].evidence)
    assert all(item.url and item.url.startswith("https://discord.com/channels/500/") for item in repeated[0].evidence)

    assert first.recommendations
    assert any(item.kind == "weekly_content" for item in first.recommendations)
    assert [item.recommendation_id for item in first.recommendations] == [
        item.recommendation_id for item in second.recommendations
    ]
    for recommendation in first.recommendations:
        assert recommendation.evidence
        assert recommendation.rationale
        assert recommendation.suggested_action
        assert recommendation.expected_outcome.startswith("Estimate")
        assert recommendation.uncertainty in {"low", "medium", "high"}


async def test_approved_knowledge_removes_only_the_gap_not_the_observation(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed(repo, index=index, questions=["How do I reset my password?"])

    before = await _pulse(repo)
    assert any(item.kind == "knowledge_gap" for item in before.observations)
    assert any(item.kind == "faq" for item in before.recommendations)

    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Password reset",
        body="How do I reset my password? Use the reset form.",
        url=None,
        approved=True,
    )

    after = await _pulse(repo)
    assert any(item.kind == "repeated_question" for item in after.observations)
    assert not any(item.kind == "faq" for item in after.recommendations)
    assert not any(
        item.kind == "knowledge_gap" and item.title == "How do I reset my password?"
        for item in after.observations
    )


async def test_recurring_problem_escalates_to_documentation_estimate(tmp_db):
    repo = Repository(tmp_db)
    for index in range(4):
        await _seed(repo, index=index, errors=["ECONNREFUSED redis:6379"])

    pulse = await _pulse(repo)

    recurring = [item for item in pulse.observations if item.kind == "recurring_problem"]
    assert recurring and recurring[0].count == 4
    recommendations = [item for item in pulse.recommendations if item.kind == "documentation"]
    assert recommendations
    assert recommendations[0].expected_outcome.startswith("Estimate (not measured)")


async def test_common_thread_name_topics_are_aggregate_only(tmp_db):
    repo = Repository(tmp_db)
    for index, name in enumerate(
        ["Login flow broken", "Login redirect loop", "Login token expired"]
    ):
        await _seed(repo, index=index, name=name)

    pulse = await _pulse(repo)

    topics = [item for item in pulse.observations if item.kind == "common_topic"]
    assert topics
    assert topics[0].title == "Common thread-name topic: login"
    assert topics[0].count == 3
    assert all(len(item.evidence) <= 5 for item in topics)


async def test_similar_discussion_reuses_fts_without_merging(tmp_db):
    repo = Repository(tmp_db)
    await _seed(repo, index=1, questions=["How do I reset my password?"])
    await _seed(repo, index=2, questions=["How do I reset my password?"])
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=1,
        guild_id="500",
        title="Earlier password thread",
        body="How do I reset my password?",
        url="https://discord.com/channels/500/1",
        approved=False,
    )

    pulse = await _pulse(repo, minimum_threads=2)

    similar = [item for item in pulse.observations if item.kind == "similar_discussion"]
    assert similar
    assert similar[0].evidence
    assert similar[0].note.startswith("Lexical BM25 match only")


async def test_guild_isolation_and_no_member_profiling(tmp_db):
    repo = Repository(tmp_db)
    for index in range(2):
        await _seed(repo, guild_id="500", index=index, questions=["Only guild A question?"])
    for index in range(4):
        await _seed(repo, guild_id="999", index=index, questions=["Only guild B question?"])

    guild_a = await _pulse(repo, guild_id="500", minimum_threads=3)
    guild_b = await _pulse(repo, guild_id="999", minimum_threads=3)

    assert guild_a.sufficient is False
    assert guild_b.sufficient is True
    refs = {
        item.ref_id
        for observation in guild_b.observations
        for item in observation.evidence
    }
    refs |= {
        item.ref_id
        for recommendation in guild_b.recommendations
        for item in recommendation.evidence
    }
    assert refs
    assert all(ref.startswith("999-") for ref in refs)
    serialized = guild_b.model_dump_json().lower()
    assert "author" not in serialized
    assert "member_name" not in serialized


async def test_recommendation_to_draft_creates_only_a_draft(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed(repo, index=index, questions=["How do I reset my password?"])
    pulse = await _pulse(repo)
    recommendation = next(item for item in pulse.recommendations if item.kind == "faq")

    settings = fake_settings(tmp_path, insights_min_threads=3)
    studio = ContentStudio(
        repo, ApprovalService(repo), settings=settings, clock=lambda: NOW
    )
    target = ContentTarget(guild_id=500, channel_id=2002, exists=True, can_send=True)

    view = await create_draft_from_recommendation(
        repo,
        studio,
        _actor(),
        recommendation_id=recommendation.recommendation_id,
        target=target,
        settings=settings,
    )

    assert view.status is DraftStatus.draft
    assert view.version == 1
    assert "Estimate" in view.content
    assert "discord.com/channels/500/" in view.content
    counts = await tmp_db.counts()
    assert counts["content_drafts"] == 1
    assert counts["content_draft_versions"] == 1
    assert counts["approval_requests"] == 0
    assert counts["approval_executions"] == 0
    assert counts["kb_docs"] == 0
    audits = await tmp_db.fetchall("SELECT action, meta_json FROM audit_log ORDER BY id")
    assert [row["action"] for row in audits] == [
        "content.draft_created",
        "pulse.draft_created",
    ]
    # Draft text is stored in the draft/version tables, never duplicated into audit metadata.
    assert all(recommendation.title not in (row["meta_json"] or "") for row in audits)


async def test_unknown_or_stale_recommendation_id_is_rejected(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await _seed(repo, index=1, questions=["How do I reset my password?"])
    settings = fake_settings(tmp_path)
    studio = ContentStudio(repo, ApprovalService(repo), settings=settings, clock=lambda: NOW)
    target = ContentTarget(guild_id=500, channel_id=2002, exists=True, can_send=True)

    with pytest.raises(PulseRecommendationNotFoundError):
        await create_draft_from_recommendation(
            repo,
            studio,
            _actor(),
            recommendation_id="pulse-deadbeef00",
            target=target,
            settings=settings,
        )

    assert (await tmp_db.counts())["content_drafts"] == 0
