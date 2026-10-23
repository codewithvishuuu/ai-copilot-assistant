"""Regression tests for `/resolve` (ModerationCog.resolve).

Same host, same reasoning as /approve-solution and /reject-solution (live
2026-10-19: event-loop stalls of 15s+ pushed a post-work reply past Discord's
3-second window). Proposing a solution writes the thread row, the solution row
and an audit row before its only reply, so the acknowledgment now leaves first -
after the auth checks, before the first state change.

Contract pinned here:
- the ack (defer) leaves BEFORE the thread row is created (the first write);
- the proposal id and the next-step instructions arrive via the follow-up window;
- denials stay direct responses and write nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeInteraction, FakeThread, fake_settings, make_user

from copilot.cogs.moderation import ModerationCog
from copilot.db.repository import Repository
from copilot.errors import NotAllowedError, NotModeratorError


def make_cog(tmp_path, db, **settings_overrides) -> ModerationCog:
    return ModerationCog(
        bot=SimpleNamespace(
            settings=fake_settings(tmp_path, **settings_overrides), repo=Repository(db)
        )
    )


def moderator_interaction() -> FakeInteraction:
    return FakeInteraction(user=make_user(7, manage_threads=True))


async def test_resolve_acks_before_the_state_change_and_replies_by_followup(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=910_001, name="Webhook outages")

    # Snapshot the row counts at the moment the ack is sent.
    counts_at_ack: list[dict] = []
    original_defer = interaction.response.defer

    async def spy_defer(*, ephemeral=False):
        counts_at_ack.append(await tmp_db.counts())
        await original_defer(ephemeral=ephemeral)

    interaction.response.defer = spy_defer

    await ModerationCog.resolve.callback(
        cog, interaction, thread, "Restart the webhook service.", None
    )

    # The ack left before any row was written - the fix's whole point.
    assert counts_at_ack[0]["threads"] == 0
    assert counts_at_ack[0]["solutions"] == 0
    assert counts_at_ack[0]["audit_log"] == 0
    assert interaction.response.deferred is True
    assert interaction.response.sent == []

    row = await tmp_db.fetchone("SELECT id FROM solutions")
    assert [m["content"] for m in interaction.followup.sent] == [
        (
            f"Solution proposed with id `{row['id']}`.\n"
            f"Approve it with `/approve-solution solution_id:{row['id']}` to add it to the "
            "knowledge base, or reject it with `/reject-solution`."
        )
    ]
    assert interaction.followup.sent[0]["ephemeral"] is True

    counts = await tmp_db.counts()
    assert counts["threads"] == 1
    assert counts["solutions"] == 1
    audits = await tmp_db.fetchall(
        "SELECT action FROM audit_log WHERE action = 'solution.proposed'"
    )
    assert len(audits) == 1


async def test_resolve_denied_user_is_answered_without_ack_or_writes(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no moderator permission
    thread = FakeThread(thread_id=910_002, name="Webhook outages")

    await ModerationCog.resolve.callback(
        cog, interaction, thread, "Restart the webhook service.", None
    )

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    assert interaction.followup.sent == []
    counts = await tmp_db.counts()
    assert counts["threads"] == 0
    assert counts["solutions"] == 0
    assert counts["audit_log"] == 0


async def test_resolve_cross_guild_target_is_denied_before_ack_or_writes(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()  # guild 500
    thread = FakeThread(thread_id=910_003, name="Other guild", guild_id=999)

    await ModerationCog.resolve.callback(
        cog, interaction, thread, "Restart the webhook service.", None
    )

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    assert interaction.followup.sent == []
    counts = await tmp_db.counts()
    assert counts["threads"] == 0
    assert counts["solutions"] == 0
    assert counts["audit_log"] == 0


async def test_resolve_off_allowlist_target_is_denied_before_ack_or_writes(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db, channel_allowlist=[42])
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=910_004, name="Elsewhere", parent_id=999)

    await ModerationCog.resolve.callback(
        cog, interaction, thread, "Restart the webhook service.", None
    )

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    assert interaction.followup.sent == []
    counts = await tmp_db.counts()
    assert counts["threads"] == 0
    assert counts["solutions"] == 0
    assert counts["audit_log"] == 0
