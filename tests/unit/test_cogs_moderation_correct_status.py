"""Handler-level regression tests for `/correct-status` (ModerationCog.correct_status).

Contract pinned here (live-verified 2026-10-19; thread "Status Correction Test"):
- a correction is a separate, append-only `corrections` row: the AI status stored
  on `summaries` is never overwritten — display layers the two (summary embed
  gains a "Moderator correction" field);
- every authorized correction writes one `status.corrected` audit row;
- `_ensure_thread_row` creates the thread row on demand when a moderator corrects
  a thread the bot has never summarized (name/guild/parent captured,
  `last_summarized_at` stays NULL — a correction is not a summary);
- every moderation touch refreshes `retention_expires_at` to now + RETENTION_DAYS
  (observed live: correction at 07:30:58Z moved expiry to 2027-01-17T07:30:58Z);
- the acknowledgment (defer) leaves before the first write and the answer travels
  through the follow-up window — the same ack-first contract as /reject-solution;
- denials write nothing at all (no correction, no audit, no thread row) and stay
  direct responses: the ack must not mask the authorization gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from discord import app_commands
from fakes import FakeInteraction, FakeThread, fake_settings, make_user

from copilot.cogs.moderation import ModerationCog
from copilot.db.repository import Repository
from copilot.db.retention import iso, utc_now
from copilot.errors import NotAllowedError, NotModeratorError

UNRESOLVED = app_commands.Choice(name="Unresolved", value="unresolved")
IN_PROGRESS = app_commands.Choice(name="In progress", value="in_progress")
RESOLVED = app_commands.Choice(name="Resolved", value="resolved")


def make_cog(tmp_path, db, **settings_overrides) -> ModerationCog:
    bot = SimpleNamespace(
        settings=fake_settings(tmp_path, **settings_overrides), repo=Repository(db)
    )
    return ModerationCog(bot=bot)


def moderator_interaction(user_id: int = 7) -> FakeInteraction:
    return FakeInteraction(user=make_user(user_id, manage_threads=True))


async def thread_row_id_for(db, discord_thread_id) -> int:
    row = await db.fetchone(
        "SELECT id FROM threads WHERE discord_thread_id = ?", (str(discord_thread_id),)
    )
    return int(row["id"])


async def test_correct_status_acks_before_the_state_change_and_saves_the_correction(
    tmp_path, tmp_db
):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=900_001, name="Status Correction Test")

    # Snapshot the row counts at the moment the ack is sent.
    counts_at_ack: list[dict] = []
    original_defer = interaction.response.defer

    async def spy_defer(*, ephemeral=False):
        counts_at_ack.append(await tmp_db.counts())
        await original_defer(ephemeral=ephemeral)

    interaction.response.defer = spy_defer

    await ModerationCog.correct_status.callback(cog, interaction, thread, IN_PROGRESS, "OP confirmed it")

    # The ack left before any row was written - the fix's whole point.
    assert counts_at_ack[0]["threads"] == 0
    assert counts_at_ack[0]["corrections"] == 0
    assert interaction.response.deferred is True
    assert interaction.response.sent == []
    assert [m["content"] for m in interaction.followup.sent] == [
        f"Correction recorded: **In progress** for <#{thread.id}>."
    ]
    assert interaction.followup.sent[0]["ephemeral"] is True

    row_id = await thread_row_id_for(tmp_db, thread.id)
    row = await tmp_db.fetchone("SELECT * FROM corrections WHERE thread_id = ?", (row_id,))
    assert row["status"] == "in_progress"  # the stored value, not the choice's display name
    assert row["actor_id"] == "7"
    assert row["note"] == "OP confirmed it"
    assert row["created_at"]


async def test_correction_writes_status_corrected_audit_entry(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=900_002, name="Status Correction Test")

    await ModerationCog.correct_status.callback(cog, interaction, thread, RESOLVED, None)

    audits = await tmp_db.fetchall("SELECT * FROM audit_log WHERE action = 'status.corrected'")
    assert len(audits) == 1
    assert audits[0]["actor_id"] == "7"
    assert audits[0]["target_type"] == "thread"
    assert audits[0]["target_id"] == str(thread.id)
    assert json.loads(audits[0]["meta_json"]) == {"status": "resolved", "guild_id": "500"}
    assert audits[0]["created_at"]


async def test_ai_summary_status_is_never_overwritten(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    thread = FakeThread(thread_id=900_003, name="Status Correction Test")
    thread_row = await repo.upsert_thread(
        discord_thread_id=str(thread.id), guild_id="500", parent_channel_id="42", name=thread.name
    )
    await repo.insert_summary(
        thread_id=thread_row,
        requested_by="7",
        model="fake-model",
        prompt_version="v1",
        status="unresolved",
        confidence=0.5,
        analysis_json="{}",
        error_signatures=[],
    )

    cog = make_cog(tmp_path, tmp_db)
    await ModerationCog.correct_status.callback(cog, moderator_interaction(), thread, IN_PROGRESS, None)

    summary = await tmp_db.fetchone(
        "SELECT status FROM summaries WHERE thread_id = ?", (thread_row,)
    )
    assert summary["status"] == "unresolved"  # the AI's stored status is untouched
    latest = await repo.latest_correction(thread_row)
    assert latest["status"] == "in_progress"  # the human record is a separate row
    summaries = await tmp_db.fetchall("SELECT id FROM summaries WHERE thread_id = ?", (thread_row,))
    assert len(summaries) == 1  # and no shadow summary was created


async def test_multiple_corrections_keep_history_and_latest_wins(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    thread = FakeThread(thread_id=900_004, name="Status Correction Test")

    for choice in (UNRESOLVED, IN_PROGRESS, RESOLVED):
        await ModerationCog.correct_status.callback(
            cog, moderator_interaction(), thread, choice, None
        )

    row_id = await thread_row_id_for(tmp_db, thread.id)
    rows = await tmp_db.fetchall(
        "SELECT status FROM corrections WHERE thread_id = ? ORDER BY id", (row_id,)
    )
    assert [r["status"] for r in rows] == ["unresolved", "in_progress", "resolved"]  # full history

    latest = await Repository(tmp_db).latest_correction(row_id)
    assert latest["status"] == "resolved"  # newest by id wins

    audits = await tmp_db.fetchall("SELECT id FROM audit_log WHERE action = 'status.corrected'")
    assert len(audits) == 3  # one audit per correction
    threads = await tmp_db.fetchall("SELECT id FROM threads")
    assert len(threads) == 1  # repeated touches reuse the same thread row


async def test_denied_non_moderator_writes_nothing(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no manage permissions, no allowlisted role
    thread = FakeThread(thread_id=900_005, name="Status Correction Test")

    await ModerationCog.correct_status.callback(cog, interaction, thread, IN_PROGRESS, None)

    assert interaction.response.deferred is None  # denials stay direct responses
    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    counts = await tmp_db.counts()
    assert counts["corrections"] == 0
    assert counts["audit_log"] == 0
    assert counts["threads"] == 0  # denied before _ensure_thread_row


async def test_denied_outside_allowlist_writes_nothing(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db, guild_allowlist=[999])
    interaction = moderator_interaction()  # moderator, but the guild is not allowlisted
    thread = FakeThread(thread_id=900_006, name="Status Correction Test")

    await ModerationCog.correct_status.callback(cog, interaction, thread, IN_PROGRESS, None)

    assert interaction.response.deferred is None  # denials stay direct responses
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    counts = await tmp_db.counts()
    assert counts["corrections"] == 0
    assert counts["audit_log"] == 0
    assert counts["threads"] == 0


async def test_cross_guild_target_is_denied_before_ack_or_writes(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()  # guild 500
    thread = FakeThread(thread_id=900_008, name="Other guild", guild_id=999)

    await ModerationCog.correct_status.callback(cog, interaction, thread, IN_PROGRESS, None)

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    counts = await tmp_db.counts()
    assert counts["corrections"] == 0
    assert counts["audit_log"] == 0
    assert counts["threads"] == 0


async def test_off_allowlist_target_is_denied_before_ack_or_writes(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db, channel_allowlist=[42])
    interaction = moderator_interaction()
    thread = FakeThread(thread_id=900_009, name="Elsewhere", parent_id=999)

    await ModerationCog.correct_status.callback(cog, interaction, thread, IN_PROGRESS, None)

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    counts = await tmp_db.counts()
    assert counts["corrections"] == 0
    assert counts["audit_log"] == 0
    assert counts["threads"] == 0


async def test_correction_for_unknown_thread_creates_the_row_on_demand(tmp_path, tmp_db):
    """Intended behavior (live 2026-10-19): a moderator may correct even a thread the
    bot has never summarized — the thread row is created on demand and linked."""
    cog = make_cog(tmp_path, tmp_db)
    thread = FakeThread(thread_id=900_007, name="Status Correction Test", guild_id=500, parent_id=42)

    await ModerationCog.correct_status.callback(cog, moderator_interaction(), thread, IN_PROGRESS, None)

    row = await tmp_db.fetchone(
        "SELECT * FROM threads WHERE discord_thread_id = ?", (str(thread.id),)
    )
    assert row["name"] == "Status Correction Test"
    assert row["guild_id"] == "500"
    assert row["parent_channel_id"] == "42"
    assert row["created_at"]
    assert row["last_summarized_at"] is None  # a correction is not a summary
    assert row["retention_expires_at"] is not None

    corr = await tmp_db.fetchone("SELECT thread_id, status FROM corrections")
    assert corr["thread_id"] == row["id"]  # linked to the freshly created row
    assert corr["status"] == "in_progress"
    counts = await tmp_db.counts()
    assert counts["summaries"] == 0
    assert counts["messages"] == 0


async def test_correction_refreshes_the_retention_window(tmp_path, tmp_db):
    """Documented side effect: every moderation touch calls `_ensure_thread_row`,
    which replaces `retention_expires_at` with now + RETENTION_DAYS (live:
    correction 2026-10-19T07:30:58Z -> expiry 2027-01-17T07:30:58Z, 90 days)."""
    repo = Repository(tmp_db)
    thread = FakeThread(thread_id=900_008, name="Status Correction Test")
    past = iso(utc_now() - timedelta(days=1))
    await repo.upsert_thread(
        discord_thread_id=str(thread.id),
        guild_id="500",
        parent_channel_id="42",
        name=thread.name,
        retention_expires_at=past,
    )

    cog = make_cog(tmp_path, tmp_db, retention_days=30)
    await ModerationCog.correct_status.callback(cog, moderator_interaction(), thread, IN_PROGRESS, None)

    row = await tmp_db.fetchone(
        "SELECT retention_expires_at FROM threads WHERE discord_thread_id = ?", (str(thread.id),)
    )
    refreshed = datetime.fromisoformat(row["retention_expires_at"])
    now = utc_now()
    assert refreshed > datetime.fromisoformat(past)  # the stale window was replaced
    assert now + timedelta(days=29) < refreshed <= now + timedelta(days=31)
