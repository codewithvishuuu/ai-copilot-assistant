"""Retention: expiry math, full per-thread purge, sweep of expired threads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from copilot.db.repository import Repository
from copilot.db.retention import (
    expiry_from_now,
    iso,
    purge_thread,
    sweep_conversation_turns,
    sweep_expired,
    utc_now,
)


def _msg(mid):
    return SimpleNamespace(
        id=mid,
        author="maya",
        created_at="2026-10-18T12:00:00+00:00",
        content="hello",
        is_opening=False,
    )


async def _seed_full_thread(repo, *, discord_id="1001", expires_at=None):
    thread_id = await repo.upsert_thread(
        discord_thread_id=discord_id,
        guild_id="500",
        parent_channel_id="42",
        name="Login is broken",
        retention_expires_at=expires_at,
    )
    await repo.store_messages(thread_id, [_msg("m1")])
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="m",
        prompt_version="v1",
        status="unresolved",
        confidence=0.5,
        analysis_json="{}",
        error_signatures=["boom"],
    )
    await repo.add_correction(thread_id=thread_id, status="resolved", actor_id="9")
    solution_id = await repo.insert_solution(
        thread_id=thread_id, body="restart the gateway", source_url=None, actor_id="9"
    )
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=thread_id,
        guild_id="500",
        title="Thread doc",
        body="login broken gateway",
        url=None,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=solution_id,
        guild_id="500",
        title="Solution doc",
        body="restart the gateway",
        url=None,
        approved=True,
    )
    return thread_id, solution_id


# ------------------------------------------------------------------ expiry math


def test_expiry_from_now_parses_and_clamps_to_one_day():
    now = utc_now()
    parsed = datetime.fromisoformat(expiry_from_now(5))
    assert now + timedelta(days=4) < parsed < now + timedelta(days=6)
    clamped = datetime.fromisoformat(expiry_from_now(0))
    assert now + timedelta(hours=23) < clamped < now + timedelta(days=1, minutes=1)


def test_iso_uses_second_precision_utc():
    moment = datetime(2026, 10, 18, 12, 0, 0, 123456, tzinfo=UTC)
    assert iso(moment) == "2026-10-18T12:00:00+00:00"


# ------------------------------------------------------------------------ purge


async def test_purge_unknown_thread_returns_empty(tmp_db):
    assert await purge_thread(tmp_db, "does-not-exist", guild_id="500") == {}


async def test_purge_thread_removes_every_trace_but_leaves_other_data(tmp_db):
    repo = Repository(tmp_db)
    thread_id, _solution_id = await _seed_full_thread(repo)
    other_thread_id = await repo.upsert_thread(
        discord_thread_id="2002", guild_id="500", parent_channel_id=None, name="Unrelated"
    )
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=other_thread_id,
        guild_id="500",
        title="Unrelated",
        body="css theming",
        url=None,
    )

    counts = await purge_thread(tmp_db, "1001", guild_id="500")
    assert counts == {"messages": 1, "summaries": 1, "corrections": 1, "solutions": 1, "kb_docs": 2}

    db_counts = await tmp_db.counts()
    assert db_counts["threads"] == 1
    assert db_counts["messages"] == 0
    assert db_counts["summaries"] == 0
    assert db_counts["corrections"] == 0
    assert db_counts["solutions"] == 0
    assert db_counts["kb_docs"] == 1  # unrelated doc survived
    assert (await repo.get_thread_row(thread_id)) is None
    assert (await repo.get_thread_row(other_thread_id)) is not None

    # FTS index follows the deletes (no ghost hits)
    assert await repo.search_kb("gateway", guild_id="500") == []
    fts_row = await tmp_db.fetchone("SELECT COUNT(*) AS c FROM kb_fts")
    assert fts_row["c"] == 1


async def test_purge_thread_with_wrong_guild_is_a_no_op(tmp_db):
    repo = Repository(tmp_db)
    await _seed_full_thread(repo)

    assert await purge_thread(tmp_db, "1001", guild_id="999") == {}
    counts = await tmp_db.counts()
    assert counts["threads"] == 1
    assert counts["kb_docs"] == 2


async def test_purge_is_idempotent(tmp_db):
    repo = Repository(tmp_db)
    await _seed_full_thread(repo)
    first = await purge_thread(tmp_db, "1001", guild_id="500")
    assert first["kb_docs"] == 2
    assert await purge_thread(tmp_db, "1001", guild_id="500") == {}


# ------------------------------------------------------------------------ sweep


async def test_sweep_expired_deletes_only_past_window(tmp_db):
    repo = Repository(tmp_db)
    now = utc_now()
    expired_id, _ = await _seed_full_thread(
        repo, discord_id="1001", expires_at=iso(now - timedelta(days=1))
    )
    future_id, _ = await _seed_full_thread(
        repo, discord_id="2002", expires_at=iso(now + timedelta(days=10))
    )
    forever_id = await repo.upsert_thread(
        discord_thread_id="3003", guild_id="500", parent_channel_id=None, name="No expiry"
    )

    counts = await sweep_expired(tmp_db, days=30, now=now)
    assert counts["messages"] == 1
    # The unreviewed thread fingerprint is removed with its thread; the approved
    # solution document is a self-contained knowledge copy that survives expiry.
    assert counts["kb_docs"] == 1

    db_counts = await tmp_db.counts()
    assert db_counts["threads"] == 2
    assert db_counts["kb_docs"] == 3  # future thread docs + the preserved approved doc
    assert (await repo.get_thread_row(expired_id)) is None
    assert (await repo.get_thread_row(future_id)) is not None
    assert (await repo.get_thread_row(forever_id)) is not None
    assert await repo.search_kb("restart the gateway", guild_id="500", approved_only=True)


async def test_sweep_returns_empty_when_nothing_expired(tmp_db):
    repo = Repository(tmp_db)
    await repo.upsert_thread(
        discord_thread_id="1001",
        guild_id="500",
        parent_channel_id=None,
        name="Fresh",
        retention_expires_at=iso(utc_now() + timedelta(days=3)),
    )
    assert await sweep_expired(tmp_db, days=30) == {}
    assert (await tmp_db.counts())["threads"] == 1


# ------------------------------------------------- conversation turns (Phase 2)


async def _insert_turn(tmp_db, *, guild="500", channel="2002", user="7", created_at: str) -> None:
    await tmp_db.execute(
        "INSERT INTO conversation_turns (guild_id, channel_id, user_id, role, content, created_at) "
        "VALUES (?, ?, ?, 'user', 'x', ?)",
        (guild, channel, user, created_at),
    )


async def test_sweep_conversation_turns_deletes_only_expired_rows(tmp_db):
    repo = Repository(tmp_db)
    now = utc_now()
    old = iso(now - timedelta(days=10))
    fresh = iso(now - timedelta(hours=2))
    for guild, channel, user, stamp in (
        ("500", "2002", "7", old),
        ("500", "2002", "7", old),
        ("500", "2002", "8", old),
        ("500", "3003", "7", fresh),
        ("999", "2002", "7", fresh),
    ):
        await _insert_turn(tmp_db, guild=guild, channel=channel, user=user, created_at=stamp)

    # Unrelated records must survive the sweep, including the FTS index.
    thread_id = await repo.upsert_thread(
        discord_thread_id="1001", guild_id="500", parent_channel_id=None, name="t"
    )
    await repo.insert_kb_doc(
        kind="thread", ref_id=thread_id, guild_id="500", title="doc", body="keep me", url=None
    )

    removed = await sweep_conversation_turns(tmp_db, days=7, now=now)
    assert removed == 3
    counts = await tmp_db.counts()
    assert counts["conversation_turns"] == 2  # both fresh rows kept
    assert counts["threads"] == 1
    assert counts["kb_docs"] == 1
    assert await repo.search_kb("keep", guild_id="500") != []


async def test_sweep_conversation_turns_clamps_window_and_is_idempotent(tmp_db):
    now = utc_now()
    await _insert_turn(tmp_db, created_at=iso(now - timedelta(days=2)))
    # days=0 clamps to one day: the 2-day-old row expires, nothing else matches.
    assert await sweep_conversation_turns(tmp_db, days=0, now=now) == 1
    assert await sweep_conversation_turns(tmp_db, days=0, now=now) == 0
