"""`/forget` authorization: tenant scope, allowlist, and moderator gate."""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeInteraction, FakeThread, fake_settings, make_user

from copilot.cogs.knowledge import KnowledgeCog
from copilot.core.approvals import (
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
)
from copilot.core.knowledge_candidates import propose_candidate
from copilot.db.repository import Repository
from copilot.errors import (
    KnowledgeDocumentNotFoundError,
    NotAllowedError,
    NotModeratorError,
)


def make_cog(tmp_path, db, **settings_overrides) -> KnowledgeCog:
    return KnowledgeCog(
        bot=SimpleNamespace(
            settings=fake_settings(tmp_path, **settings_overrides),
            repo=Repository(db),
            db=db,
        )
    )


def moderator_interaction() -> FakeInteraction:
    return FakeInteraction(user=make_user(7, manage_threads=True))


async def seed_thread(db, *, discord_id: str = "1001", guild_id: str = "500") -> int:
    repo = Repository(db)
    thread_id = await repo.upsert_thread(
        discord_thread_id=discord_id,
        guild_id=guild_id,
        parent_channel_id="42",
        name="Login is broken",
    )
    await repo.store_messages(
        thread_id,
        [
            SimpleNamespace(
                id=discord_id,
                author="maya",
                created_at="2026-10-18T12:00:00+00:00",
                content="opening post",
                is_opening=True,
            )
        ],
    )
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=thread_id,
        guild_id=guild_id,
        title="Login doc",
        body="gateway login broken",
        url=None,
    )
    return thread_id


async def test_kb_search_returns_only_the_interaction_guild(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Guild A fix",
        body="gateway restart",
        url=None,
        approved=True,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=2,
        guild_id="999",
        title="Guild B fix",
        body="gateway restart",
        url=None,
        approved=True,
    )
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction(guild_id=500)

    await KnowledgeCog.kb_search.callback(cog, interaction, "gateway")

    sent = repr([embed.to_dict() for embed in interaction.response.sent[-1]["embeds"]])
    assert "Guild A fix" in sent
    assert "Guild B fix" not in sent


async def test_forget_same_guild_still_purges_the_thread(tmp_path, tmp_db):
    await seed_thread(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=1001, guild_id=500, parent_id=42)

    await KnowledgeCog.forget.callback(cog, interaction, thread)

    counts = await tmp_db.counts()
    assert counts["threads"] == 0
    assert counts["messages"] == 0
    assert counts["kb_docs"] == 0
    assert "Deleted all stored data" in interaction.response.sent[-1]["content"]


async def seed_approved_doc(db, *, guild_id: str = "500") -> int:
    return await Repository(db).insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id=guild_id,
        title="Approved fix",
        body="clear the cache and restart",
        url=None,
        approved=True,
    )


async def test_candidate_approve_command_is_moderator_gated(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    thread_id = await seed_thread(tmp_db)
    candidate = await propose_candidate(
        ApprovalActor(
            actor_id="7",
            guild_id=500,
            kind=ActorKind.human,
            permissions=frozenset({ApprovalPermission.propose}),
        ),
        repo,
        thread_row_id=thread_id,
        body="Restart the gateway once.",
    )
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no moderator rights

    await KnowledgeCog.knowledge_candidate_approve.callback(cog, interaction, candidate.candidate_id)

    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    assert (await repo.get_solution(candidate.candidate_id))["state"] == "proposed"
    assert (await tmp_db.counts())["kb_docs"] == 1  # only the seeded thread fingerprint


async def test_knowledge_purge_removes_the_exact_approved_document(tmp_path, tmp_db):
    doc_id = await seed_approved_doc(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()

    await KnowledgeCog.knowledge_purge.callback(cog, interaction, doc_id)

    assert "Permanently purged" in interaction.followup.sent[-1]["content"]
    assert (await tmp_db.counts())["kb_docs"] == 0
    audits = await tmp_db.fetchall("SELECT action FROM audit_log")
    assert [row["action"] for row in audits] == ["knowledge.document_purged"]


async def test_knowledge_purge_is_denied_for_non_moderators(tmp_path, tmp_db):
    doc_id = await seed_approved_doc(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no moderator rights

    await KnowledgeCog.knowledge_purge.callback(cog, interaction, doc_id)

    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    assert (await tmp_db.counts())["kb_docs"] == 1
    assert (await tmp_db.counts())["audit_log"] == 0


async def test_knowledge_purge_is_cross_guild_scoped(tmp_path, tmp_db):
    doc_id = await seed_approved_doc(tmp_db, guild_id="999")
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()  # guild 500

    await KnowledgeCog.knowledge_purge.callback(cog, interaction, doc_id)

    assert interaction.followup.sent[-1]["content"] == str(KnowledgeDocumentNotFoundError())
    assert (await tmp_db.counts())["kb_docs"] == 1
    assert (await tmp_db.counts())["audit_log"] == 0


async def test_forget_cross_guild_is_denied_and_changes_nothing(tmp_path, tmp_db):
    await seed_thread(tmp_db, guild_id="999")
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()  # guild 500
    thread = FakeThread(thread_id=1001, guild_id=999, parent_id=42)

    await KnowledgeCog.forget.callback(cog, interaction, thread)

    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    counts = await tmp_db.counts()
    assert counts["threads"] == 1
    assert counts["messages"] == 1
    assert counts["kb_docs"] == 1
    assert counts["audit_log"] == 0


async def test_forget_off_allowlist_target_is_denied_and_changes_nothing(tmp_path, tmp_db):
    await seed_thread(tmp_db)
    cog = make_cog(tmp_path, tmp_db, channel_allowlist=[42])
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=1001, guild_id=500, parent_id=999)

    await KnowledgeCog.forget.callback(cog, interaction, thread)

    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    counts = await tmp_db.counts()
    assert counts["threads"] == 1
    assert counts["messages"] == 1
    assert counts["kb_docs"] == 1
    assert counts["audit_log"] == 0


async def test_forget_denied_non_moderator_changes_nothing(tmp_path, tmp_db):
    await seed_thread(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no moderator rights
    thread = FakeThread(thread_id=1001, guild_id=500, parent_id=42)

    await KnowledgeCog.forget.callback(cog, interaction, thread)

    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    counts = await tmp_db.counts()
    assert counts["threads"] == 1
    assert counts["messages"] == 1
    assert counts["kb_docs"] == 1
    assert counts["audit_log"] == 0
