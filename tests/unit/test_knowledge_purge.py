"""Approved KB document purge: authorization, guild scope, FTS and rollback."""

from __future__ import annotations

import asyncio

import pytest

from copilot.core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from copilot.core.knowledge_purge import purge_approved_document
from copilot.db.repository import Repository
from copilot.errors import (
    ApprovalDeniedError,
    DatabaseError,
    KnowledgeDocumentNotApprovedError,
    KnowledgeDocumentNotFoundError,
)

DECIDE = frozenset({ApprovalPermission.decide})


def actor(
    *,
    actor_id: str = "7",
    guild_id: int = 500,
    permissions=DECIDE,
    kind: ActorKind = ActorKind.human,
) -> ApprovalActor:
    return ApprovalActor(
        actor_id=actor_id, guild_id=guild_id, kind=kind, permissions=frozenset(permissions)
    )


async def seed_doc(
    repo: Repository,
    *,
    guild_id: str = "500",
    approved: bool = True,
    title: str = "Approved cache fix",
    body: str = "clear the cache and restart the gateway",
    ref_id: int | None = 1,
) -> int:
    return await repo.insert_kb_doc(
        kind="solution",
        ref_id=ref_id,
        guild_id=guild_id,
        title=title,
        body=body,
        url="https://discord.com/channels/500/1001",
        approved=approved,
    )


async def test_successful_purge_removes_row_fts_and_audits_without_content(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo)
    assert await repo.search_kb("clear cache gateway", guild_id="500", approved_only=True)

    result = await purge_approved_document(actor(), repo, document_id=doc_id)

    assert result.document_id == doc_id
    assert result.kind == "solution"
    assert result.ref_id == 1
    assert result.body_chars == len("clear the cache and restart the gateway")
    assert await repo.search_kb("clear cache gateway", guild_id="500", approved_only=True) == []
    fts_count = await tmp_db.fetchone("SELECT COUNT(*) AS c FROM kb_fts")
    assert fts_count["c"] == 0
    assert (await tmp_db.counts())["kb_docs"] == 0
    assert await tmp_db.fetchall("PRAGMA foreign_key_check") == []

    audits = await tmp_db.fetchall(
        "SELECT actor_id, action, target_type, target_id, meta_json FROM audit_log"
    )
    assert len(audits) == 1
    audit = audits[0]
    assert audit["actor_id"] == "7"
    assert audit["action"] == "knowledge.document_purged"
    assert audit["target_type"] == "kb_doc"
    assert audit["target_id"] == str(doc_id)
    assert "clear the cache" not in audit["meta_json"]
    assert "Approved cache fix" not in audit["meta_json"]
    assert "discord.com" not in audit["meta_json"]
    assert '"body_chars": 39' in audit["meta_json"]


async def test_unapproved_document_is_refused(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo, approved=False)

    with pytest.raises(KnowledgeDocumentNotApprovedError):
        await purge_approved_document(actor(), repo, document_id=doc_id)

    assert (await tmp_db.counts())["kb_docs"] == 1
    assert (await tmp_db.counts())["audit_log"] == 0


async def test_missing_and_cross_guild_documents_are_not_found(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo, guild_id="999")

    with pytest.raises(KnowledgeDocumentNotFoundError):
        await purge_approved_document(actor(guild_id=500), repo, document_id=doc_id)
    with pytest.raises(KnowledgeDocumentNotFoundError):
        await purge_approved_document(actor(), repo, document_id=9999)

    assert (await tmp_db.counts())["kb_docs"] == 1
    assert (await tmp_db.counts())["audit_log"] == 0


async def test_unauthorized_actors_are_denied(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo)

    with pytest.raises(ApprovalDeniedError):
        await purge_approved_document(actor(permissions=frozenset()), repo, document_id=doc_id)
    with pytest.raises(ApprovalDeniedError):
        await purge_approved_document(
            actor(kind=ActorKind.agent), repo, document_id=doc_id
        )

    assert (await tmp_db.counts())["kb_docs"] == 1
    assert (await tmp_db.counts())["audit_log"] == 0


async def test_orphan_approved_document_can_be_purged(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo, ref_id=None)

    result = await purge_approved_document(actor(), repo, document_id=doc_id)

    assert result.ref_id is None
    assert (await tmp_db.counts())["kb_docs"] == 0


async def test_failed_audit_rolls_back_the_delete_and_keeps_fts(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo)
    await tmp_db.execute(
        "CREATE TRIGGER audit_down BEFORE INSERT ON audit_log "
        "WHEN NEW.action = 'knowledge.document_purged' "
        "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
    )

    with pytest.raises(DatabaseError):
        await purge_approved_document(actor(), repo, document_id=doc_id)

    assert (await tmp_db.counts())["kb_docs"] == 1
    assert (await tmp_db.counts())["audit_log"] == 0
    assert await repo.search_kb("clear cache gateway", guild_id="500", approved_only=True)


async def test_concurrent_purges_have_one_winner_and_one_audit(tmp_db):
    repo = Repository(tmp_db)
    doc_id = await seed_doc(repo)

    results = await asyncio.gather(
        purge_approved_document(actor(actor_id="7"), repo, document_id=doc_id),
        purge_approved_document(actor(actor_id="8"), repo, document_id=doc_id),
        return_exceptions=True,
    )
    successes = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], KnowledgeDocumentNotFoundError)
    assert (await tmp_db.counts())["kb_docs"] == 0
    audits = await tmp_db.fetchall("SELECT action FROM audit_log")
    assert [row["action"] for row in audits] == ["knowledge.document_purged"]
