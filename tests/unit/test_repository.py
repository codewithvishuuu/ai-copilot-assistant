"""Repository: parameterized SQL, upserts, excerpts, summaries, FTS search, audit."""

from __future__ import annotations

import json
from types import SimpleNamespace

from copilot.db.repository import Repository, utc_now_iso


def _msg(mid, *, content="hello", author="maya", opening=False):
    return SimpleNamespace(
        id=mid,
        author=author,
        created_at="2026-10-18T12:00:00+00:00",
        content=content,
        is_opening=opening,
    )


async def _seed_thread(repo, *, discord_id="1001", name="Login is broken", guild="500"):
    return await repo.upsert_thread(
        discord_thread_id=discord_id, guild_id=guild, parent_channel_id="42", name=name
    )


# ---------------------------------------------------------------------- threads


async def test_upsert_thread_creates_then_updates_in_place(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    row = await repo.get_thread_by_discord_id("1001")
    assert row["id"] == thread_id
    assert row["name"] == "Login is broken"

    again = await repo.upsert_thread(
        discord_thread_id="1001", guild_id="500", parent_channel_id="99", name="Renamed"
    )
    assert again == thread_id  # no duplicate row
    row = await repo.get_thread_by_discord_id("1001")
    assert row["name"] == "Renamed"
    assert row["parent_channel_id"] == "99"
    assert (await tmp_db.counts())["threads"] == 1


async def test_upsert_thread_retention_coalesce_keeps_previous_value(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    first = "2030-01-01T00:00:00+00:00"
    await repo.upsert_thread(
        discord_thread_id="1001",
        guild_id="500",
        parent_channel_id=None,
        name="x",
        retention_expires_at=first,
    )
    assert (await repo.get_thread_row(thread_id))["retention_expires_at"] == first
    await repo.upsert_thread(
        discord_thread_id="1001", guild_id="500", parent_channel_id=None, name="x"
    )
    assert (await repo.get_thread_row(thread_id))["retention_expires_at"] == first


async def test_touch_thread_summarized_sets_timestamp(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    assert (await repo.get_thread_row(thread_id))["last_summarized_at"] is None
    await repo.touch_thread_summarized(thread_id)
    assert (await repo.get_thread_row(thread_id))["last_summarized_at"] is not None


# --------------------------------------------------------------------- messages


async def test_store_messages_replaces_excerpt_and_cascades(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    assert await repo.store_messages(thread_id, [_msg("m1"), _msg("m2", opening=True)]) == 2
    assert (await tmp_db.counts())["messages"] == 2

    assert await repo.store_messages(thread_id, [_msg("m3")]) == 1
    rows = await tmp_db.fetchall(
        "SELECT message_id, is_opening FROM messages WHERE thread_id = ?", (thread_id,)
    )
    assert [r["message_id"] for r in rows] == ["m3"]

    await tmp_db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    assert (await tmp_db.counts())["messages"] == 0  # FK cascade


# -------------------------------------------------------------------- summaries


async def test_insert_summary_and_latest_summary(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="gemma4:e2b",
        prompt_version="v1",
        status="unresolved",
        confidence=0.6,
        analysis_json="{}",
        error_signatures=["mysql: lost connection"],
    )
    second = await repo.insert_summary(
        thread_id=thread_id,
        requested_by="8",
        model="gemma4:e2b",
        prompt_version="v1",
        status="resolved",
        confidence=0.9,
        analysis_json='{"a": 1}',
        error_signatures=[],
    )
    latest = await repo.latest_summary(thread_id)
    assert latest["id"] == second
    assert latest["status"] == "resolved"
    assert latest["requested_by"] == "8"
    assert json.loads(latest["error_signatures"]) == []


async def test_summaries_since_filters_by_time_and_limit(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by=None,
        model="m",
        prompt_version="v1",
        status="resolved",
        confidence=1.0,
        analysis_json="{}",
        error_signatures=[],
    )
    rows = await repo.summaries_since("2000-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["thread_id"] == thread_id
    assert await repo.summaries_since("2999-01-01T00:00:00+00:00") == []
    assert await repo.summaries_since("2000-01-01T00:00:00+00:00", limit=0) == []


# ------------------------------------------------------------------ corrections


async def test_corrections_roundtrip(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    assert await repo.latest_correction(thread_id) is None
    await repo.add_correction(thread_id=thread_id, status="resolved", actor_id="9", note="confirmed fixed")
    latest = await repo.latest_correction(thread_id)
    assert latest["status"] == "resolved"
    assert latest["actor_id"] == "9"
    assert latest["note"] == "confirmed fixed"
    assert latest["created_at"]


# ---------------------------------------------------------------- knowledge base


async def test_kb_doc_exists_and_delete_removes_fts_rows(tmp_db):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(kind="solution", ref_id=5, title="t", body="b", url=None)
    assert await repo.kb_doc_exists(kind="solution", ref_id=5) is True
    assert await repo.kb_doc_exists(kind="thread", ref_id=5) is False

    assert await repo.delete_kb_docs(kind="solution", ref_id=5) == 1
    assert await repo.kb_doc_exists(kind="solution", ref_id=5) is False
    assert (await tmp_db.counts())["kb_docs"] == 0
    fts_row = await tmp_db.fetchone("SELECT COUNT(*) AS c FROM kb_fts")
    assert fts_row["c"] == 0  # trigger kept the index in sync


async def test_search_kb_ranks_matches_and_returns_snippets(tmp_db):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        title="SQLite journal mode",
        body="The sqlite database returns locked errors under concurrent writes; enable WAL.",
        url=None,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=2,
        title="Discord rate limits",
        body="Respect the retry-after header when sending messages.",
        url=None,
    )
    rows = await repo.search_kb("database locked")
    assert rows
    assert rows[0]["title"] == "SQLite journal mode"
    assert "[" in rows[0]["snippet"] and "]" in rows[0]["snippet"]
    assert rows[0]["score"] is not None


async def test_search_kb_returns_empty_for_unusable_query(tmp_db):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(kind="solution", ref_id=1, title="Anything", body="something", url=None)
    assert await repo.search_kb("a of it") == []
    assert await repo.search_kb("!!!") == []


async def test_search_kb_survives_injection_attempts(tmp_db):
    repo = Repository(tmp_db)
    await repo.insert_kb_doc(kind="solution", ref_id=1, title="Safe", body="normal content", url=None)
    rows = await repo.search_kb('error"; DROP TABLE kb_docs; --')
    assert isinstance(rows, list)
    assert (await tmp_db.counts())["kb_docs"] == 1  # table intact


async def test_related_docs_excludes_the_source_thread(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo, discord_id="777", name="Database locked on write")
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=thread_id,
        title="Database locked on write",
        body="sqlite database locked when writing",
        url=None,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        title="Enable WAL",
        body="sqlite database locking fix: enable WAL mode",
        url=None,
    )
    rows = await repo.related_docs("database locked", exclude_discord_thread_id="777")
    titles = [r["title"] for r in rows]
    assert "Database locked on write" not in titles
    assert "Enable WAL" in titles

    rows = await repo.related_docs("database locked")
    by_title = {r["title"]: r for r in rows}
    assert by_title["Database locked on write"]["source_thread"] == "777"
    assert by_title["Enable WAL"]["source_thread"] is None


# ------------------------------------------------------------------------- audit


async def test_audit_serializes_meta_as_json(tmp_db):
    repo = Repository(tmp_db)
    row_id = await repo.audit(
        actor_id="7", action="test.action", target_type="thread", target_id="1001", meta={"k": "v"}
    )
    row = await tmp_db.fetchone("SELECT * FROM audit_log WHERE id = ?", (row_id,))
    assert row["action"] == "test.action"
    assert row["target_type"] == "thread"
    assert json.loads(row["meta_json"]) == {"k": "v"}
    assert row["created_at"]


async def test_audit_defaults_meta_to_empty_object(tmp_db):
    repo = Repository(tmp_db)
    row_id = await repo.audit(actor_id="7", action="bare")
    row = await tmp_db.fetchone("SELECT meta_json FROM audit_log WHERE id = ?", (row_id,))
    assert row["meta_json"] == "{}"


def test_utc_now_iso_format_is_second_precision():
    stamp = utc_now_iso()
    assert stamp.endswith("+00:00")
    assert "T" in stamp
    assert "." not in stamp  # timespec="seconds"
