"""Schema migrations: v2 -> v10 upgrade keeps all data; fresh init; integrity.

Migrations 3 through 6 add conversation memory, guild/review metadata for the
knowledge base, the Phase 2A approval tables, and the Phase 2B Content Studio
tables. Migration 7 adds nullable source/version metadata to existing solution
candidates; migration 8 adds missions, research reports, planning tables,
offline evaluation runs, draft provenance and guild-scoped audit; migration 9
adds the durable scheduler jobs table; migration 10 rebuilds the agent run
tables to admit the read-only evaluation host mode. These tests prove that an
existing version-2 database upgrades in place without touching its data or the
FTS index, that migration 4 backfills ``guild_id`` and ``approved`` correctly,
that a version-9 agent run and its step survive the v10 rebuild, that a fresh
database reaches v10 with all tables present, and that SQLite reports full
integrity afterwards.

The v2 fixture is a genuinely created version-2 database in a temp directory —
never the real ``data/copilot.db``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import copilot
from copilot.db.database import Database, parse_migrations
from copilot.db.repository import Repository
from copilot.errors import DatabaseError

SCHEMA_PATH = Path(copilot.__file__).parent / "db" / "schema.sql"


def _schema_upto(version: int) -> str:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    return "\n".join(sql for number, sql in parse_migrations(text) if number <= version)


def _make_v2_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_schema_upto(2))
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _seed_v2_data(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO threads (discord_thread_id, guild_id, parent_channel_id, name, "
            "created_at, retention_expires_at) VALUES ('1001', '500', '42', "
            "'Login is broken', '2026-10-18T12:00:00+00:00', '2030-01-01T00:00:00+00:00')"
        )
        thread_row = conn.execute(
            "SELECT id FROM threads WHERE discord_thread_id = '1001'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO messages (thread_id, message_id, content, is_opening) "
            "VALUES (?, 'm1', 'hello', 1)",
            (thread_row,),
        )
        conn.execute(
            "INSERT INTO summaries (thread_id, created_at, model, prompt_version, status, "
            "analysis_json) VALUES (?, '2026-10-18T12:00:00+00:00', 'm', 'v1', 'resolved', '{}')",
            (thread_row,),
        )
        conn.execute(
            "INSERT INTO solutions (thread_id, body, state, proposed_by, proposed_at) "
            "VALUES (?, 'clear cookies', 'approved', '9', '2026-10-18T12:00:00+00:00')",
            (thread_row,),
        )
        solution_row = conn.execute("SELECT id FROM solutions").fetchone()[0]
        conn.execute(
            "INSERT INTO kb_docs (kind, ref_id, title, body, created_at) "
            "VALUES ('thread', ?, 'Login doc', 'gateway login broken', "
            "'2026-10-18T12:00:00+00:00')",
            (thread_row,),
        )
        conn.execute(
            "INSERT INTO kb_docs (kind, ref_id, title, body, created_at) "
            "VALUES ('solution', ?, 'Solution doc', 'clear cookies', "
            "'2026-10-18T12:00:00+00:00')",
            (solution_row,),
        )
        conn.execute(
            "INSERT INTO audit_log (created_at, actor_id, action) "
            "VALUES ('2026-10-18T12:00:00+00:00', '9', 'seed')"
        )
        conn.commit()
    finally:
        conn.close()


def _object_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    finally:
        conn.close()


async def test_upgrade_from_v2_preserves_everything_and_adds_scoped_knowledge(tmp_path):
    path = tmp_path / "v2.db"
    _make_v2_database(path)
    _seed_v2_data(path)

    db = Database(path)
    await db.connect()
    try:
        assert await db.user_version() == 10
        counts = await db.counts()
        assert counts["threads"] == 1
        assert counts["messages"] == 1
        assert counts["summaries"] == 1
        assert counts["solutions"] == 1
        assert counts["kb_docs"] == 2
        assert counts["audit_log"] == 1
        assert counts["conversation_turns"] == 0

        rows = await db.fetchall(
            "SELECT kind, guild_id, approved FROM kb_docs ORDER BY kind"
        )
        assert [(row["kind"], row["guild_id"], row["approved"]) for row in rows] == [
            ("solution", "500", 1),
            ("thread", "500", 0),
        ]

        repo = Repository(db)
        # The FTS index survived the migration and still matches.
        titles = [
            row["title"]
            for row in await repo.search_kb(
                "gateway", guild_id="500", approved_only=False
            )
        ]
        assert titles == ["Login doc"]
        # The migrated unapproved row stays hidden from default retrieval.
        assert await repo.search_kb("gateway", guild_id="500") == []

        # The new table is immediately usable through the repository.
        await repo.append_conversation_exchange(
            guild_id="500",
            channel_id="2002",
            user_id="7",
            question="q",
            answer="a",
            keep_last=40,
        )
        rows = await repo.recent_conversation_turns(
            guild_id="500", channel_id="2002", user_id="7", limit=10
        )
        assert [row["role"] for row in rows] == ["user", "assistant"]

        # SQLite integrity + foreign-key consistency after the migration.
        integrity = await db.fetchone("PRAGMA integrity_check")
        assert integrity[0] == "ok"
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()


async def test_migrations_3_through_10_keep_objects_and_data(tmp_path):
    """Every pre-existing object survives; migration 10 rebuilds only agent tables."""
    path = tmp_path / "additive.db"
    _make_v2_database(path)
    before = _object_names(path)

    db = Database(path)
    await db.connect()
    try:
        assert await db.user_version() == 10
        columns = {
            row["name"]
            for row in await db.fetchall("PRAGMA table_info(kb_docs)")
        }
        assert {"guild_id", "approved"} <= columns
        execution_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(approval_executions)")}
        assert "result_ref" in execution_columns
        solution_columns = {
            row["name"] for row in await db.fetchall("PRAGMA table_info(solutions)")
        }
        assert {"content_hash", "source_summary_id", "source_kind"} <= solution_columns
        defaults = {
            row["name"]: row["dflt_value"]
            for row in await db.fetchall("PRAGMA table_info(solutions)")
        }
        assert defaults["source_kind"] == "'manual'"
        draft_columns = {
            row["name"]: row["dflt_value"]
            for row in await db.fetchall("PRAGMA table_info(content_drafts)")
        }
        assert {"source_kind", "source_ref"} <= set(draft_columns)
        assert draft_columns["source_kind"] == "'manual'"
        audit_columns = {
            row["name"] for row in await db.fetchall("PRAGMA table_info(audit_log)")
        }
        assert "guild_id" in audit_columns
    finally:
        await db.close()

    after = _object_names(path)
    assert after - before == {
        "agent_eval_runs",
        "agent_run_steps",
        "agent_runs",
        "approval_executions",
        "approval_requests",
        "attachments",
        "content_calendar_entries",
        "content_draft_versions",
        "content_drafts",
        "content_feedback",
        "contextual_replies",
        "conversation_turns",
        "event_plans",
        "guild_voice_profiles",
        "idx_agent_eval_runs_guild",
        "idx_agent_run_steps_run",
        "idx_agent_runs_guild",
        "idx_agent_runs_idempotency",
        "idx_agent_runs_status",
        "idx_approval_executions_approval",
        "idx_approval_executions_key",
        "idx_approval_requests_due",
        "idx_approval_requests_guild",
        "idx_attachments_created",
        "idx_attachments_message",
        "idx_attachments_scope",
        "idx_audit_log_guild",
        "idx_calendar_guild_start",
        "idx_content_draft_versions_hash",
        "idx_content_drafts_approval",
        "idx_content_drafts_guild",
        "idx_content_drafts_source",
        "idx_content_feedback_guild",
        "idx_contextual_replies_guild",
        "idx_conversation_turns_created",
        "idx_conversation_turns_scope",
        "idx_event_plans_guild",
        "idx_kb_docs_guild",
        "idx_mission_steps_mission",
        "idx_missions_guild_created",
        "idx_missions_guild_status",
        "idx_missions_idempotency",
        "idx_moderation_incidents_author",
        "idx_moderation_incidents_guild",
        "idx_moderation_warnings_user",
        "idx_reply_approvals_guild",
        "idx_research_reports_guild",
        "idx_research_reports_mission",
        "idx_scheduled_jobs_due",
        "idx_scheduled_jobs_guild",
        "idx_solutions_hash",
        "idx_solutions_thread",
        "mission_steps",
        "missions",
        "moderation_incidents",
        "moderation_policies",
        "moderation_warnings",
        "reply_approvals",
        "research_reports",
        "scheduled_jobs",
        "sqlite_autoindex_agent_run_steps_1",
        "sqlite_autoindex_content_draft_versions_1",
        "sqlite_autoindex_guild_voice_profiles_1",
        "sqlite_autoindex_mission_steps_1",
        "sqlite_autoindex_moderation_incidents_1",
        "sqlite_autoindex_moderation_policies_1",
        "sqlite_autoindex_moderation_warnings_1",
        "sqlite_autoindex_scheduled_jobs_1",
    }
    assert before - after == set()


async def test_v10_rebuild_preserves_agent_runs_and_steps_and_allows_evaluation(tmp_path):
    path = tmp_path / "agent-runs-v9.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_schema_upto(9))
        conn.execute("PRAGMA user_version = 9")
        conn.execute(
            "INSERT INTO agent_runs (guild_id, channel_id, requester_id, goal, mode, "
            "status, trace_id, created_at, updated_at) "
            "VALUES ('500', '2002', '7', 'old goal', 'community', 'completed', "
            "'trace-old', '2026-10-20T00:00:00+00:00', '2026-10-20T00:00:01+00:00')"
        )
        run_id = conn.execute("SELECT id FROM agent_runs").fetchone()[0]
        conn.execute(
            "INSERT INTO agent_run_steps (run_id, step_index, role_id, title, skill_id, "
            "status) VALUES (?, 0, 'community_manager', 'Old step', 'synthesis.brief', "
            "'succeeded')",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await db.user_version() == 10
        rows = await db.fetchall("SELECT id, mode, goal, trace_id FROM agent_runs")
        assert [(row["id"], row["mode"], row["goal"], row["trace_id"]) for row in rows] == [
            (run_id, "community", "old goal", "trace-old")
        ]
        steps = await db.fetchall("SELECT run_id, step_index, skill_id FROM agent_run_steps")
        assert [(row["run_id"], row["step_index"], row["skill_id"]) for row in steps] == [
            (run_id, 0, "synthesis.brief")
        ]

        await db.execute(
            "INSERT INTO agent_runs (guild_id, channel_id, requester_id, goal, mode, "
            "trace_id, created_at, updated_at) "
            "VALUES ('500', '2002', '7', 'evaluate', 'evaluation', "
            "'trace-new', '2026-10-20T00:00:02+00:00', '2026-10-20T00:00:02+00:00')"
        )
        new_run = await db.fetchone("SELECT id FROM agent_runs WHERE trace_id = 'trace-new'")
        assert new_run is not None
        await db.execute(
            "INSERT INTO agent_run_steps (run_id, step_index, role_id, title, skill_id) "
            "VALUES (?, 0, 'quality_evaluation', 'Checks', 'quality.checks')",
            (new_run["id"],),
        )
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        assert (await db.fetchone("PRAGMA integrity_check"))[0] == "ok"
    finally:
        await db.close()


async def test_failed_migration_rolls_back_and_leaves_prior_version_usable(tmp_path):
    """A mid-block failure must not leave a half-applied migration or open connection."""
    path = tmp_path / "failed-migration.db"
    _make_v2_database(path)
    _seed_v2_data(path)

    # Force migration 7 to fail after its first ALTER TABLE succeeds: the second
    # ALTER then collides with this pre-existing column. The whole block must roll
    # back, including the first added column.
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE solutions ADD COLUMN source_summary_id INTEGER")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    with pytest.raises(DatabaseError):
        await db.connect()

    assert await db.ping() is False  # failed startup releases the connection

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {row[1] for row in raw.execute("PRAGMA table_info(solutions)")}
        assert "content_hash" not in columns  # rolled back with migration 7
        assert "source_kind" not in columns
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Earlier migrations and seeded data survived; the FTS index still matches.
        assert raw.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0] == 2
    finally:
        raw.close()


async def test_upgraded_database_keeps_working_after_reconnect(tmp_path):
    path = tmp_path / "reconnect.db"
    _make_v2_database(path)
    first = Database(path)
    await first.connect()
    await first.close()

    second = Database(path)
    await second.connect()
    try:
        assert await second.user_version() == 10
    finally:
        await second.close()


async def test_fresh_database_reaches_v10_with_full_integrity(tmp_db):
    assert await tmp_db.user_version() == 10
    assert (await tmp_db.counts())["conversation_turns"] == 0
    assert (await tmp_db.counts())["approval_requests"] == 0
    assert (await tmp_db.counts())["content_drafts"] == 0
    assert (await tmp_db.counts())["content_draft_versions"] == 0
    assert (await tmp_db.counts())["missions"] == 0
    assert (await tmp_db.counts())["mission_steps"] == 0
    assert (await tmp_db.counts())["research_reports"] == 0
    assert (await tmp_db.counts())["event_plans"] == 0
    assert (await tmp_db.counts())["content_calendar_entries"] == 0
    assert (await tmp_db.counts())["contextual_replies"] == 0
    assert (await tmp_db.counts())["agent_eval_runs"] == 0
    assert (await tmp_db.fetchone("PRAGMA integrity_check"))[0] == "ok"
    assert await tmp_db.fetchall("PRAGMA foreign_key_check") == []
