"""Database facade: migrations, transactions, parameterization, failure mapping."""

from __future__ import annotations

import pytest

from copilot.db.database import Database, parse_migrations
from copilot.errors import DatabaseError

# ------------------------------------------------------------------ migrations


def test_parse_migrations_splits_blocks_and_strips():
    text = (
        "-- Community Copilot schema\n"
        "-- @migration 1\n"
        "CREATE TABLE a(x);\n"
        "-- @migration 2\n"
        "CREATE TABLE b(x);\n"
    )
    assert parse_migrations(text) == [
        (1, "CREATE TABLE a(x);"),
        (2, "CREATE TABLE b(x);"),
    ]


def test_parse_migrations_rejects_duplicates_and_disorder():
    with pytest.raises(DatabaseError):
        parse_migrations("-- @migration 1\nSELECT 1;\n-- @migration 1\nSELECT 2;")
    with pytest.raises(DatabaseError):
        parse_migrations("-- @migration 2\nSELECT 2;\n-- @migration 1\nSELECT 1;")


def test_parse_real_schema_has_nine_ordered_migrations():
    from pathlib import Path

    import copilot

    schema_path = Path(copilot.__file__).parent / "db" / "schema.sql"
    migrations = parse_migrations(schema_path.read_text(encoding="utf-8"))
    versions = [version for version, _ in migrations]
    assert versions == sorted(versions)
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


# --------------------------------------------------------------- connect / ops


async def test_connect_applies_migrations_and_reports_version(tmp_db):
    assert await tmp_db.user_version() == 10
    counts = await tmp_db.counts()
    for table in (
        "threads",
        "messages",
        "summaries",
        "corrections",
        "solutions",
        "kb_docs",
        "audit_log",
        "conversation_turns",
        "approval_requests",
        "approval_executions",
        "content_drafts",
        "content_draft_versions",
        "missions",
        "mission_steps",
        "research_reports",
        "event_plans",
        "content_calendar_entries",
        "contextual_replies",
        "agent_eval_runs",
        "attachments",
        "guild_voice_profiles",
        "content_feedback",
        "moderation_policies",
        "moderation_incidents",
        "moderation_warnings",
        "agent_runs",
        "agent_run_steps",
        "scheduled_jobs",
        "reply_approvals",
    ):
        assert table in counts
        assert counts[table] == 0


async def test_connect_is_idempotent_on_second_instance(tmp_path):
    path = tmp_path / "again.db"
    first = Database(path)
    await first.connect()
    await first.execute(
        "INSERT INTO audit_log (created_at, actor_id, action) VALUES ('t', 'a', 'x')"
    )
    await first.close()

    second = Database(path)
    await second.connect()
    try:
        assert await second.user_version() == 10
        counts = await second.counts()
        assert counts["audit_log"] == 1  # data survived; migrations did not re-run
    finally:
        await second.close()


async def test_insert_execute_fetch_roundtrip(tmp_db):
    rowid = await tmp_db.insert(
        "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, meta_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "1", "test", None, None, "{}"),
    )
    assert rowid > 0
    row = await tmp_db.fetchone("SELECT * FROM audit_log WHERE id = ?", (rowid,))
    assert row["action"] == "test"

    updated = await tmp_db.execute(
        "UPDATE audit_log SET action = ? WHERE id = ?", ("renamed", rowid)
    )
    assert updated == 1


async def test_parameters_neutralize_hostile_content(tmp_db):
    hostile = "'); DROP TABLE audit_log; --"
    rowid = await tmp_db.insert(
        "INSERT INTO audit_log (created_at, actor_id, action) VALUES (?, ?, ?)",
        ("t", "a", hostile),
    )
    row = await tmp_db.fetchone("SELECT action FROM audit_log WHERE id = ?", (rowid,))
    assert row["action"] == hostile
    assert "audit_log" in await tmp_db.counts()  # table intact


async def test_run_in_transaction_commits_all_or_nothing(tmp_db):
    await tmp_db.run_in_transaction(
        [
            ("INSERT INTO audit_log (created_at, actor_id, action) VALUES (?, ?, ?)", ("t", "a", "one")),
            ("INSERT INTO audit_log (created_at, actor_id, action) VALUES (?, ?, ?)", ("t", "a", "two")),
        ]
    )
    assert (await tmp_db.counts())["audit_log"] == 2

    with pytest.raises(DatabaseError):
        await tmp_db.run_in_transaction(
            [
                ("INSERT INTO audit_log (created_at, actor_id, action) VALUES (?, ?, ?)", ("t", "a", "three")),
                ("INSERT INTO no_such_table (x) VALUES (1)", ()),
            ]
        )
    assert (await tmp_db.counts())["audit_log"] == 2  # rolled back


async def test_sql_errors_map_to_database_error(tmp_db):
    with pytest.raises(DatabaseError):
        await tmp_db.execute("SELECT * FROM no_such_table")
    with pytest.raises(DatabaseError):
        await tmp_db.insert("INSERT INTO audit_log (nope) VALUES (1)")


async def test_foreign_key_cascade_deletes_messages(tmp_db):
    thread_id = await tmp_db.insert(
        "INSERT INTO threads (discord_thread_id, guild_id, name, created_at) VALUES (?, ?, ?, ?)",
        ("111", "500", "t", "2026-01-01T00:00:00+00:00"),
    )
    await tmp_db.insert(
        "INSERT INTO messages (thread_id, message_id, content) VALUES (?, ?, ?)",
        (thread_id, "m1", "hello"),
    )
    await tmp_db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    assert (await tmp_db.counts())["messages"] == 0


async def test_close_marks_database_unusable(tmp_path):
    db = Database(tmp_path / "closed.db")
    await db.connect()
    await db.close()
    assert await db.ping() is False
    with pytest.raises(DatabaseError):
        await db.fetchone("SELECT 1")


async def test_counts_never_leak_sql_injection_from_table_names(tmp_db):
    # sanity: counts() only ever queries its fixed table list
    counts = await tmp_db.counts()
    assert set(counts) == {
        "threads",
        "messages",
        "summaries",
        "corrections",
        "solutions",
        "kb_docs",
        "audit_log",
        "conversation_turns",
        "approval_requests",
        "approval_executions",
        "content_drafts",
        "content_draft_versions",
        "missions",
        "mission_steps",
        "research_reports",
        "event_plans",
        "content_calendar_entries",
        "contextual_replies",
        "agent_eval_runs",
        "attachments",
        "guild_voice_profiles",
        "content_feedback",
        "moderation_policies",
        "moderation_incidents",
        "moderation_warnings",
        "agent_runs",
        "agent_run_steps",
        "scheduled_jobs",
        "reply_approvals",
    }
