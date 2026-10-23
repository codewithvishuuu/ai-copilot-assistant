"""Living knowledge base: candidate source validation, review and retention."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from copilot.core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from copilot.core.knowledge_candidates import (
    accept_proposal,
    candidate_findings,
    discover_from_summary,
    list_candidates,
    propose_candidate,
    review_candidate,
)
from copilot.db.repository import Repository
from copilot.db.retention import expiry_from_now, iso, sweep_expired
from copilot.errors import (
    ApprovalDeniedError,
    KnowledgeCandidateConflictError,
    KnowledgeCandidateDuplicateError,
    KnowledgeCandidateNotFoundError,
    KnowledgeCandidateSourceError,
    KnowledgeCandidateStaleError,
)

ALL = frozenset({ApprovalPermission.propose, ApprovalPermission.decide})
REVIEW = frozenset({ApprovalPermission.decide})
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def actor(
    *,
    actor_id: str = "7",
    guild_id: int = 500,
    permissions=ALL,
    kind: ActorKind = ActorKind.human,
) -> ApprovalActor:
    return ApprovalActor(
        actor_id=actor_id, guild_id=guild_id, kind=kind, permissions=frozenset(permissions)
    )


async def seed_thread(repo: Repository, *, guild_id: str = "500", index: int = 1) -> int:
    return await repo.upsert_thread(
        discord_thread_id=f"{guild_id}-{index}",
        guild_id=guild_id,
        parent_channel_id="42",
        name=f"thread {index}",
    )


async def seed_summary(
    repo: Repository,
    thread_id: int,
    *,
    body: str = "restart the gateway",
    status: str = "resolved",
) -> int:
    return await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="test-model",
        prompt_version="test-v1",
        status=status,
        confidence=0.5,
        analysis_json=json.dumps(
            {"attempted_solutions": [{"text": body, "source_message_ids": []}]}
        ),
        error_signatures=[],
    )


async def test_candidate_is_stored_unapproved_and_not_searchable(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)

    view = await propose_candidate(
        actor(),
        repo,
        thread_row_id=thread_id,
        body="Restart the gateway after clearing the cache.",
    )

    assert view.state == "proposed"
    assert view.source_kind == "manual"
    assert view.content_hash
    assert view.guild_id == "500"
    assert await repo.search_kb("restart gateway cache", guild_id="500", approved_only=True) == []
    assert (await tmp_db.counts())["kb_docs"] == 0
    audits = await tmp_db.fetchall("SELECT action, meta_json FROM audit_log")
    assert [row["action"] for row in audits] == ["knowledge.candidate.proposed"]
    assert "Restart the gateway" not in audits[0]["meta_json"]


async def test_source_summary_must_exist_and_belong_to_the_caller_guild(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    summary_id = await seed_summary(repo, thread_id)

    other_thread = await seed_thread(repo, guild_id="999", index=2)
    other_summary = await seed_summary(repo, other_thread)

    with pytest.raises(KnowledgeCandidateSourceError):
        await propose_candidate(
            actor(),
            repo,
            thread_row_id=thread_id,
            body="Use the fix.",
            source_summary_id=other_summary,
            source_kind="summary",
        )
    with pytest.raises(KnowledgeCandidateSourceError):
        await propose_candidate(
            actor(),
            repo,
            thread_row_id=other_thread,
            body="Use the fix.",
            source_summary_id=other_summary,
            source_kind="summary",
        )
    with pytest.raises(KnowledgeCandidateSourceError):
        await propose_candidate(
            actor(),
            repo,
            thread_row_id=thread_id,
            body="Use the fix.",
            source_summary_id=9999,
            source_kind="summary",
        )
    with pytest.raises(KnowledgeCandidateSourceError):
        await propose_candidate(
            actor(), repo, thread_row_id=thread_id, body="Use the fix.", source_kind="summary"
        )
    assert (await tmp_db.counts())["solutions"] == 0
    assert summary_id


async def test_identical_candidate_is_deduplicated(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    body = "Set the worker concurrency to two."

    first = await propose_candidate(actor(), repo, thread_row_id=thread_id, body=body)
    with pytest.raises(KnowledgeCandidateDuplicateError):
        await propose_candidate(actor(), repo, thread_row_id=thread_id, body=body)

    assert (await tmp_db.counts())["solutions"] == 1
    shown = await list_candidates(actor(), repo)
    assert [item.candidate_id for item in shown] == [first.candidate_id]


async def test_same_thread_conflicting_wording_is_flagged_not_overwritten(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)

    first = await propose_candidate(
        actor(), repo, thread_row_id=thread_id, body="set timeout to 30 seconds"
    )
    second = await propose_candidate(
        actor(), repo, thread_row_id=thread_id, body="set timeout to 60 seconds"
    )

    assert any(item.code == "possible_contradiction" for item in second.findings)
    assert any(item.candidate_id == first.candidate_id for item in second.findings)
    assert not second.findings[0].message.startswith("This is a verified")

    await review_candidate(actor(), repo, candidate_id=first.candidate_id, decision="approve")
    await review_candidate(actor(), repo, candidate_id=second.candidate_id, decision="approve")

    docs = await tmp_db.fetchall(
        "SELECT ref_id, body FROM kb_docs WHERE kind = 'solution' ORDER BY ref_id"
    )
    assert len(docs) == 2
    assert docs[0]["body"] == "set timeout to 30 seconds"
    assert docs[1]["body"] == "set timeout to 60 seconds"


async def test_unauthorized_review_is_denied_and_changes_nothing(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    view = await propose_candidate(actor(), repo, thread_row_id=thread_id, body="Apply the fix.")

    with pytest.raises(ApprovalDeniedError):
        await review_candidate(
            actor(permissions=frozenset({ApprovalPermission.propose})),
            repo,
            candidate_id=view.candidate_id,
            decision="approve",
        )
    with pytest.raises(ApprovalDeniedError):
        await review_candidate(
            actor(kind=ActorKind.agent),
            repo,
            candidate_id=view.candidate_id,
            decision="approve",
        )

    row = await repo.get_solution(view.candidate_id)
    assert row["state"] == "proposed"
    assert (await tmp_db.counts())["kb_docs"] == 0


async def test_cross_guild_review_and_listing_are_isolated(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    view = await propose_candidate(actor(), repo, thread_row_id=thread_id, body="Apply the fix.")

    with pytest.raises(KnowledgeCandidateNotFoundError):
        await review_candidate(
            actor(guild_id=999), repo, candidate_id=view.candidate_id, decision="approve"
        )
    with pytest.raises(KnowledgeCandidateNotFoundError):
        await review_candidate(
            actor(guild_id=999), repo, candidate_id=view.candidate_id, decision="reject"
        )

    assert await list_candidates(actor(guild_id=999), repo) == []
    assert [item.candidate_id for item in await list_candidates(actor(), repo)] == [
        view.candidate_id
    ]
    assert (await repo.get_solution(view.candidate_id))["state"] == "proposed"


async def test_stale_summary_source_blocks_approval(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    summary_id = await seed_summary(repo, thread_id, body="restart the gateway")
    view = await propose_candidate(
        actor(),
        repo,
        thread_row_id=thread_id,
        body="Restart the gateway.",
        source_summary_id=summary_id,
        source_kind="summary",
    )
    await seed_summary(repo, thread_id, body="a newer answer")

    assert (await list_candidates(actor(), repo))[0].stale is True
    with pytest.raises(KnowledgeCandidateStaleError):
        await review_candidate(actor(), repo, candidate_id=view.candidate_id, decision="approve")

    assert (await repo.get_solution(view.candidate_id))["state"] == "proposed"
    assert (await tmp_db.counts())["kb_docs"] == 0


async def test_concurrent_approve_and_reject_have_one_winner(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    view = await propose_candidate(actor(), repo, thread_row_id=thread_id, body="Apply the fix.")

    results = await asyncio.gather(
        review_candidate(actor(), repo, candidate_id=view.candidate_id, decision="approve"),
        review_candidate(actor(actor_id="8"), repo, candidate_id=view.candidate_id, decision="reject"),
        return_exceptions=True,
    )
    successes = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], KnowledgeCandidateConflictError)
    row = await repo.get_solution(view.candidate_id)
    assert row["state"] in {"approved", "rejected"}
    docs = await tmp_db.fetchall("SELECT id FROM kb_docs WHERE kind = 'solution'")
    assert len(docs) == (1 if row["state"] == "approved" else 0)
    audits = await tmp_db.fetchall(
        "SELECT action FROM audit_log WHERE action LIKE 'knowledge.candidate.%'"
    )
    assert len(audits) == 2  # proposed + the single winning decision


async def test_identical_body_across_guilds_is_not_a_duplicate(tmp_db):
    repo = Repository(tmp_db)
    thread_a = await seed_thread(repo, guild_id="500", index=1)
    thread_b = await seed_thread(repo, guild_id="999", index=2)

    body = "Apply the same documented fix."
    await propose_candidate(actor(), repo, thread_row_id=thread_a, body=body)
    other = await propose_candidate(
        actor(guild_id=999), repo, thread_row_id=thread_b, body=body
    )

    assert other.findings == []
    assert len(await list_candidates(actor(), repo)) == 1
    assert len(await list_candidates(actor(guild_id=999), repo)) == 1


async def test_discovered_summary_candidate_is_a_proposal_until_accepted(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    summary_id = await seed_summary(repo, thread_id, body="Rotate the API key.")

    proposal = await discover_from_summary(repo, guild_id="500", summary_id=summary_id)

    assert proposal.body == "Rotate the API key."
    assert proposal.source_summary_id == summary_id
    assert (await tmp_db.counts())["solutions"] == 0

    view = await accept_proposal(actor(), repo, proposal)

    assert view.state == "proposed"
    assert view.source_kind == "summary"
    assert view.source_summary_id == summary_id
    assert (await tmp_db.counts())["kb_docs"] == 0


async def test_missing_discovery_source_is_rejected(tmp_db):
    repo = Repository(tmp_db)
    with pytest.raises(KnowledgeCandidateSourceError):
        await discover_from_summary(repo, guild_id="500", summary_id=404)

    thread_id = await seed_thread(repo, guild_id="999")
    summary_id = await seed_summary(repo, thread_id)
    with pytest.raises(KnowledgeCandidateSourceError):
        await discover_from_summary(repo, guild_id="500", summary_id=summary_id)


async def test_findings_are_lexical_and_bounded(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Approved cache fix",
        body="clear the cache and restart",
        url=None,
        approved=True,
    )

    findings = await candidate_findings(
        repo,
        guild_id="500",
        thread_row_id=thread_id,
        body="clear the cache and restart",
    )

    codes = {item.code for item in findings}
    assert "related_approved_knowledge" in codes
    assert len(findings) <= 5


async def test_retention_removes_pending_candidates_but_keeps_approved_knowledge(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(repo)
    await repo.upsert_thread(
        discord_thread_id="500-1",
        guild_id="500",
        parent_channel_id="42",
        name="thread 1",
        retention_expires_at=iso(NOW - timedelta(days=1)),
    )
    summary_id = await seed_summary(repo, thread_id, body="Restart the gateway.")
    approved = await propose_candidate(
        actor(),
        repo,
        thread_row_id=thread_id,
        body="Restart the gateway exactly once.",
        source_summary_id=summary_id,
        source_kind="summary",
    )
    await review_candidate(actor(), repo, candidate_id=approved.candidate_id, decision="approve")
    pending = await propose_candidate(
        actor(), repo, thread_row_id=thread_id, body="Try a different gateway setting."
    )

    counts = await sweep_expired(tmp_db, days=30, now=NOW)

    assert counts["solutions"] == 2  # both candidate rows left with the thread
    assert (await repo.get_solution(approved.candidate_id)) is None
    assert (await repo.get_solution(pending.candidate_id)) is None
    assert (await tmp_db.counts())["threads"] == 0
    docs = await tmp_db.fetchall("SELECT body, approved FROM kb_docs")
    assert [(row["body"], row["approved"]) for row in docs] == [
        ("Restart the gateway exactly once.", 1)
    ]
    assert await repo.search_kb("restart gateway", guild_id="500", approved_only=True)
    audits = await tmp_db.fetchall("SELECT action FROM audit_log")
    assert len(audits) >= 3  # proposal, approval, and repository audit evidence remain
    assert expiry_from_now(1)
