"""Parameterized data access. All SQL fragments used by features live here.

Rules:
- every statement is parameterized (``?`` placeholders) — no string-built SQL
  from user or model content;
- multi-statement workflows go through ``Database.run_in_transaction``;
- rows are returned as ``sqlite3.Row`` (dict-like).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..util.text import build_fts_query
from .database import Database


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ threads

    async def upsert_thread(
        self,
        *,
        discord_thread_id: str,
        guild_id: str,
        parent_channel_id: str | None,
        name: str,
        retention_expires_at: str | None = None,
    ) -> int:
        existing = await self.db.fetchone(
            "SELECT id FROM threads WHERE discord_thread_id = ?", (discord_thread_id,)
        )
        if existing is not None:
            await self.db.execute(
                "UPDATE threads SET guild_id = ?, parent_channel_id = ?, name = ?, "
                "retention_expires_at = COALESCE(?, retention_expires_at) WHERE id = ?",
                (guild_id, parent_channel_id, name, retention_expires_at, existing["id"]),
            )
            return int(existing["id"])
        return await self.db.insert(
            "INSERT INTO threads (discord_thread_id, guild_id, parent_channel_id, name, "
            "created_at, retention_expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                discord_thread_id,
                guild_id,
                parent_channel_id,
                name,
                utc_now_iso(),
                retention_expires_at,
            ),
        )

    async def get_thread_by_discord_id(self, discord_thread_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM threads WHERE discord_thread_id = ?", (discord_thread_id,)
        )

    async def get_thread_row(self, thread_row_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone("SELECT * FROM threads WHERE id = ?", (thread_row_id,))

    async def touch_thread_summarized(self, thread_row_id: int) -> None:
        await self.db.execute(
            "UPDATE threads SET last_summarized_at = ? WHERE id = ?",
            (utc_now_iso(), thread_row_id),
        )

    # ----------------------------------------------------------------- messages

    async def store_messages(self, thread_row_id: int, messages: list[Any]) -> int:
        """Replace the stored excerpt for a thread with ``messages`` (bounded by collector)."""
        statements: list[tuple[str, tuple]] = [
            ("DELETE FROM messages WHERE thread_id = ?", (thread_row_id,))
        ]
        for message in messages:
            statements.append(
                (
                    "INSERT INTO messages (thread_id, message_id, author_id, author_name, "
                    "created_at, content, is_opening) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        thread_row_id,
                        message.id,
                        None,
                        message.author,
                        message.created_at,
                        message.content,
                        1 if message.is_opening else 0,
                    ),
                )
            )
        await self.db.run_in_transaction(statements)
        return len(messages)

    # ---------------------------------------------------------------- summaries

    async def insert_summary(
        self,
        *,
        thread_id: int,
        requested_by: str | None,
        model: str,
        prompt_version: str,
        status: str,
        confidence: float,
        analysis_json: str,
        error_signatures: list[str],
    ) -> int:
        return await self.db.insert(
            "INSERT INTO summaries (thread_id, created_at, requested_by, model, "
            "prompt_version, status, confidence, analysis_json, error_signatures) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                utc_now_iso(),
                requested_by,
                model,
                prompt_version,
                status,
                confidence,
                analysis_json,
                json.dumps(error_signatures),
            ),
        )

    async def latest_summary(self, thread_row_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM summaries WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
            (thread_row_id,),
        )

    async def summaries_since(
        self, since_iso: str, *, guild_id: str, limit: int = 1000
    ) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT s.id, s.thread_id, s.created_at, s.status, s.error_signatures, "
            "s.analysis_json FROM summaries s JOIN threads t ON t.id = s.thread_id "
            "WHERE t.guild_id = ? AND s.created_at >= ? ORDER BY s.id DESC LIMIT ?",
            (str(guild_id), since_iso, limit),
        )

    async def pulse_summaries_since(
        self, since_iso: str, *, guild_id: str, limit: int = 1000
    ) -> list[sqlite3.Row]:
        """Guild-scoped summaries with the thread facts Pulse may cite.

        Author/member columns are deliberately absent: Pulse aggregates threads
        only and never profiles or ranks individual members.
        """
        return await self.db.fetchall(
            "SELECT s.id, s.thread_id, s.created_at, s.status, s.error_signatures, "
            "s.analysis_json, t.guild_id, t.discord_thread_id, t.name AS thread_name "
            "FROM summaries s JOIN threads t ON t.id = s.thread_id "
            "WHERE t.guild_id = ? AND s.created_at >= ? ORDER BY s.id DESC LIMIT ?",
            (str(guild_id), since_iso, limit),
        )

    async def get_summary_for_guild(
        self, summary_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        """A summary only when its thread belongs to ``guild_id``."""
        return await self.db.fetchone(
            "SELECT s.*, t.guild_id, t.discord_thread_id, t.name AS thread_name "
            "FROM summaries s JOIN threads t ON t.id = s.thread_id "
            "WHERE s.id = ? AND t.guild_id = ?",
            (int(summary_id), str(guild_id)),
        )

    # -------------------------------------------------------------- corrections

    async def add_correction(
        self,
        *,
        thread_id: int,
        status: str,
        actor_id: str,
        note: str | None = None,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO corrections (thread_id, status, actor_id, created_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, status, actor_id, utc_now_iso(), note),
        )

    async def latest_correction(self, thread_row_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT status, actor_id, created_at, note FROM corrections "
            "WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
            (thread_row_id,),
        )

    # ---------------------------------------------------------------- solutions

    async def insert_solution(
        self,
        *,
        thread_id: int,
        body: str,
        source_url: str | None,
        actor_id: str,
        content_hash: str | None = None,
        source_summary_id: int | None = None,
        source_kind: str = "manual",
    ) -> int:
        return await self.db.insert(
            "INSERT INTO solutions (thread_id, body, source_url, state, proposed_by, proposed_at, "
            "content_hash, source_summary_id, source_kind) "
            "VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?)",
            (
                thread_id,
                body,
                source_url,
                actor_id,
                utc_now_iso(),
                content_hash,
                source_summary_id,
                source_kind,
            ),
        )

    async def get_solution(self, solution_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone("SELECT * FROM solutions WHERE id = ?", (solution_id,))

    async def get_solution_for_guild(
        self, solution_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        """A solution only when its thread belongs to ``guild_id`` (no existence leak)."""
        return await self.db.fetchone(
            "SELECT s.* FROM solutions s JOIN threads t ON t.id = s.thread_id "
            "WHERE s.id = ? AND t.guild_id = ?",
            (solution_id, str(guild_id)),
        )

    async def get_candidate_for_guild(
        self, solution_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        """One solution plus its thread facts, scoped to ``guild_id``."""
        return await self.db.fetchone(
            "SELECT s.*, t.guild_id AS thread_guild_id, t.discord_thread_id, "
            "t.name AS thread_name FROM solutions s JOIN threads t ON t.id = s.thread_id "
            "WHERE s.id = ? AND t.guild_id = ?",
            (int(solution_id), str(guild_id)),
        )

    async def solutions_for_guild(
        self, *, guild_id: str, state: str | None = None, limit: int = 200
    ) -> list[sqlite3.Row]:
        """Guild-scoped solutions, newest first, optionally filtered by state."""
        base = (
            "SELECT s.*, t.guild_id AS thread_guild_id, t.discord_thread_id, "
            "t.name AS thread_name FROM solutions s JOIN threads t ON t.id = s.thread_id "
            "WHERE t.guild_id = ?"
        )
        if state is None:
            sql = base + " ORDER BY s.id DESC LIMIT ?"
            params: tuple = (str(guild_id), int(limit))
        else:
            sql = base + " AND s.state = ? ORDER BY s.id DESC LIMIT ?"
            params = (str(guild_id), state, int(limit))
        return await self.db.fetchall(sql, params)

    async def solutions_for_thread(
        self, *, guild_id: str, thread_row_id: int, limit: int = 100
    ) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT s.* FROM solutions s JOIN threads t ON t.id = s.thread_id "
            "WHERE t.guild_id = ? AND s.thread_id = ? ORDER BY s.id DESC LIMIT ?",
            (str(guild_id), int(thread_row_id), int(limit)),
        )

    async def set_solution_state(
        self,
        solution_id: int,
        *,
        state: str,
        decided_by: str,
        note: str | None = None,
        expected_state: str | None = None,
    ) -> int:
        """Update a solution; optionally require its current state atomically."""
        if expected_state is None:
            return await self.db.execute(
                "UPDATE solutions SET state = ?, decided_by = ?, decided_at = ?, "
                "note = COALESCE(?, note) WHERE id = ?",
                (state, decided_by, utc_now_iso(), note, solution_id),
            )
        return await self.db.execute(
            "UPDATE solutions SET state = ?, decided_by = ?, decided_at = ?, "
            "note = COALESCE(?, note) WHERE id = ? AND state = ?",
            (state, decided_by, utc_now_iso(), note, solution_id, expected_state),
        )

    async def approve_solution_with_kb(
        self, solution_id: int, *, guild_id: str, actor_id: str
    ) -> str:
        """Approve a guild-owned proposed solution and publish its KB row atomically.

        Returns ``approved``, ``not_found``, ``already_<state>``, or ``stale``.
        A candidate tied to a source summary can only be approved while that
        summary is still the thread's latest; a newer summary therefore blocks
        an outdated candidate rather than silently publishing it. The existing
        approved solution is never updated: approval only inserts a new
        ``kb_docs`` row for this solution id.
        """
        now = utc_now_iso()
        meta = json.dumps({"state": "approved", "guild_id": str(guild_id)})

        def _run(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                "SELECT s.*, t.guild_id AS thread_guild_id, t.discord_thread_id, "
                "t.name AS thread_name FROM solutions s JOIN threads t ON t.id = s.thread_id "
                "WHERE s.id = ? AND t.guild_id = ?",
                (int(solution_id), str(guild_id)),
            ).fetchone()
            if row is None:
                return "not_found"
            if row["state"] != "proposed":
                return f"already_{row['state']}"
            source_summary_id = row["source_summary_id"]
            if source_summary_id is not None:
                latest = conn.execute(
                    "SELECT MAX(id) AS latest FROM summaries WHERE thread_id = ?",
                    (int(row["thread_id"]),),
                ).fetchone()
                if latest is None or int(latest["latest"]) != int(source_summary_id):
                    return "stale"

            cursor = conn.execute(
                "UPDATE solutions SET state = 'approved', decided_by = ?, decided_at = ? "
                "WHERE id = ? AND state = 'proposed'",
                (actor_id, now, int(solution_id)),
            )
            if cursor.rowcount != 1:
                return "conflict"

            title = (
                f"Solution: {row['thread_name']}" if row["thread_name"] else "Solution"
            )
            thread_url = (
                f"https://discord.com/channels/{row['thread_guild_id']}/{row['discord_thread_id']}"
                if row["thread_guild_id"] and row["discord_thread_id"]
                else None
            )
            conn.execute(
                "INSERT INTO kb_docs (kind, ref_id, guild_id, approved, title, body, url, "
                "created_at) VALUES ('solution', ?, ?, 1, ?, ?, ?, ?)",
                (
                    int(solution_id),
                    str(guild_id),
                    title,
                    row["body"],
                    row["source_url"] or thread_url,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'solution.approved', 'solution', ?, ?, ?)",
                (now, actor_id, str(solution_id), meta, str(guild_id)),
            )
            return "approved"

        return await self.db.run_in_transaction_with_result(_run)

    # -------------------------------------------------------------- knowledge base

    async def insert_kb_doc(
        self,
        *,
        kind: str,
        ref_id: int | None,
        guild_id: str,
        title: str,
        body: str,
        url: str | None,
        approved: bool = False,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO kb_docs (kind, ref_id, guild_id, approved, title, body, url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                ref_id,
                str(guild_id),
                1 if approved else 0,
                title,
                body,
                url,
                utc_now_iso(),
            ),
        )

    async def delete_kb_docs(self, *, kind: str, ref_id: int) -> int:
        return await self.db.execute(
            "DELETE FROM kb_docs WHERE kind = ? AND ref_id = ?", (kind, ref_id)
        )

    async def kb_doc_exists(self, *, kind: str, ref_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 AS x FROM kb_docs WHERE kind = ? AND ref_id = ? LIMIT 1", (kind, ref_id)
        )
        return row is not None

    async def search_kb(
        self, text: str, *, guild_id: str, limit: int = 5, approved_only: bool = True
    ) -> list[sqlite3.Row]:
        """Search guild-scoped knowledge; approval is the default retrieval gate.

        Callers running an explicitly authorized review workflow (candidate
        listing, moderation evidence) must pass ``approved_only=False``. Every
        user-facing search uses the default.
        """
        match = build_fts_query(text)
        if match is None:
            return []
        return await self.db.fetchall(
            "SELECT kb_docs.id, kb_docs.kind, kb_docs.title, kb_docs.body, kb_docs.url, "
            "kb_docs.approved, kb_docs.created_at, "
            "snippet(kb_fts, 1, '[', ']', ' … ', 14) AS snippet, "
            "bm25(kb_fts) AS score "
            "FROM kb_fts JOIN kb_docs ON kb_docs.id = kb_fts.rowid "
            "WHERE kb_fts MATCH ? AND kb_docs.guild_id = ? "
            "AND (? = 0 OR kb_docs.approved = 1) ORDER BY score LIMIT ?",
            (match, str(guild_id), 1 if approved_only else 0, int(limit)),
        )

    async def related_docs(
        self,
        text: str,
        *,
        guild_id: str,
        exclude_discord_thread_id: str | None = None,
        limit: int = 3,
        approved_only: bool = True,
    ) -> list[sqlite3.Row]:
        """Related knowledge for user-facing summaries; approved-only by default."""
        match = build_fts_query(text)
        if match is None:
            return []
        return await self.db.fetchall(
            "SELECT kb_docs.id, kb_docs.kind, kb_docs.title, kb_docs.body, kb_docs.url, "
            "kb_docs.approved, kb_docs.created_at, "
            "snippet(kb_fts, 1, '[', ']', ' … ', 12) AS snippet, "
            "bm25(kb_fts) AS score, t.discord_thread_id AS source_thread "
            "FROM kb_fts JOIN kb_docs ON kb_docs.id = kb_fts.rowid "
            "LEFT JOIN threads t ON kb_docs.kind = 'thread' AND t.id = kb_docs.ref_id "
            "WHERE kb_fts MATCH ? AND kb_docs.guild_id = ? "
            "AND (? = 0 OR kb_docs.approved = 1) "
            "AND (t.discord_thread_id IS NULL OR t.discord_thread_id <> ?) "
            "ORDER BY score LIMIT ?",
            (
                match,
                str(guild_id),
                1 if approved_only else 0,
                exclude_discord_thread_id or "",
                int(limit),
            ),
        )

    async def purge_approved_kb_doc(
        self, kb_doc_id: int, *, guild_id: str, actor_id: str
    ) -> tuple[str, dict]:
        """Permanently delete one approved guild-owned KB document and audit it.

        Returns ``("deleted", facts)``, ``("not_found", {})`` or
        ``("not_approved", {})``. The delete, its FTS trigger, and the audit row
        share one transaction: a failed audit leaves the document intact. The
        audit metadata records ids/counts only - never title, URL or body text.
        """
        now = utc_now_iso()

        def _run(conn: sqlite3.Connection) -> tuple[str, dict]:
            row = conn.execute(
                "SELECT id, kind, ref_id, approved, length(body) AS body_chars "
                "FROM kb_docs WHERE id = ? AND guild_id = ?",
                (int(kb_doc_id), str(guild_id)),
            ).fetchone()
            if row is None:
                return "not_found", {}
            if int(row["approved"]) != 1:
                return "not_approved", {}

            cursor = conn.execute(
                "DELETE FROM kb_docs WHERE id = ? AND guild_id = ? AND approved = 1",
                (int(kb_doc_id), str(guild_id)),
            )
            if cursor.rowcount != 1:
                # Another reviewer deleted it between the read and the delete.
                return "not_found", {}

            facts = {
                "document_id": int(row["id"]),
                "kind": str(row["kind"]),
                "ref_id": int(row["ref_id"]) if row["ref_id"] is not None else None,
                "body_chars": int(row["body_chars"] or 0),
            }
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'knowledge.document_purged', 'kb_doc', ?, ?, ?)",
                (
                    now,
                    actor_id,
                    str(kb_doc_id),
                    json.dumps({"guild_id": str(guild_id), **facts}),
                    str(guild_id),
                ),
            )
            return "deleted", facts

        return await self.db.run_in_transaction_with_result(_run)

    # -------------------------------------------------------------------- audit

    async def audit(
        self,
        *,
        actor_id: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        meta: dict | None = None,
        guild_id: str | None = None,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
            "meta_json, guild_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                utc_now_iso(),
                actor_id,
                action,
                target_type,
                target_id,
                json.dumps(meta or {}),
                "" if guild_id is None else str(guild_id),
            ),
        )

    # ------------------------------------------------------- conversation memory

    async def append_conversation_exchange(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        question: str,
        answer: str,
        keep_last: int,
    ) -> None:
        """Store one (user, assistant) exchange and prune the scope atomically.

        The prune keeps at most ``keep_last`` rows per scope, so an active
        conversation stays bounded between retention sweeps. All statements run
        in one transaction: a failure stores nothing.
        """
        insert = (
            "INSERT INTO conversation_turns (guild_id, channel_id, user_id, role, content, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)"
        )
        prune = (
            "DELETE FROM conversation_turns WHERE guild_id = ? AND channel_id = ? "
            "AND user_id = ? AND id NOT IN (SELECT id FROM conversation_turns "
            "WHERE guild_id = ? AND channel_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?)"
        )
        now = utc_now_iso()
        scope = (guild_id, channel_id, user_id)
        await self.db.run_in_transaction(
            [
                (insert, (*scope, "user", question, now)),
                (insert, (*scope, "assistant", answer, now)),
                (prune, (*scope, *scope, int(keep_last))),
            ]
        )

    async def recent_conversation_turns(
        self, *, guild_id: str, channel_id: str, user_id: str, limit: int
    ) -> list[sqlite3.Row]:
        """The newest ``limit`` turns for one scope, returned oldest-first."""
        rows = await self.db.fetchall(
            "SELECT id, role, content, created_at FROM conversation_turns "
            "WHERE guild_id = ? AND channel_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (guild_id, channel_id, user_id, int(limit)),
        )
        rows.reverse()
        return rows

    async def delete_conversation_turns(
        self, *, guild_id: str, channel_id: str, user_id: str
    ) -> int:
        """Delete one scope entirely (a single atomic statement); returns rows removed."""
        return await self.db.execute(
            "DELETE FROM conversation_turns WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )

    async def conversation_turn_stats(
        self, *, guild_id: str, channel_id: str, user_id: str
    ) -> sqlite3.Row | None:
        """Count and timestamp bounds for one scope; never returns turn content."""
        return await self.db.fetchone(
            "SELECT COUNT(*) AS turns, MIN(created_at) AS oldest, MAX(created_at) AS newest "
            "FROM conversation_turns WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )

    # --------------------------------------------------------------- approvals

    async def insert_approval_request(
        self,
        *,
        action_type: str,
        guild_id: str,
        target_type: str,
        target_id: str,
        content_version: str,
        content_hash: str,
        requested_by: str,
        requester_kind: str,
        created_at: str,
        expires_at: str,
    ) -> int:
        """Create a pending request and its audit event atomically."""
        meta = json.dumps(
            {
                "action_type": action_type,
                "guild_id": str(guild_id),
                "target_type": target_type,
                "target_id": target_id,
                "content_version": content_version,
                "content_hash": content_hash,
                "requester_kind": requester_kind,
                "expires_at": expires_at,
            }
        )

        def _run(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO approval_requests (action_type, guild_id, target_type, target_id, "
                "content_version, content_hash, requested_by, requester_kind, state, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    action_type,
                    str(guild_id),
                    target_type,
                    target_id,
                    content_version,
                    content_hash,
                    requested_by,
                    requester_kind,
                    created_at,
                    expires_at,
                ),
            )
            approval_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'approval.requested', 'approval', ?, ?, ?)",
                (created_at, requested_by, str(approval_id), meta, str(guild_id)),
            )
            return approval_id

        return await self.db.run_in_transaction_with_result(_run)

    async def get_approval_request(
        self, approval_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        """A request only when it belongs to ``guild_id`` (no existence leak)."""
        return await self.db.fetchone(
            "SELECT * FROM approval_requests WHERE id = ? AND guild_id = ?",
            (int(approval_id), str(guild_id)),
        )

    async def decide_approval_request(
        self,
        approval_id: int,
        *,
        guild_id: str,
        state: str,
        decided_by: str,
        decided_at: str,
        note: str | None,
        now: str,
    ) -> bool:
        """Conditionally decide a still-pending, unexpired request.

        The ``state = 'pending'`` guard makes concurrent decisions a single
        winner. The audit row is in the same transaction as the state change.
        """
        meta = json.dumps({"state": state})

        def _run(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE approval_requests SET state = ?, decided_by = ?, decided_at = ?, "
                "decision_note = ? WHERE id = ? AND guild_id = ? AND state = 'pending' "
                "AND expires_at > ?",
                (state, decided_by, decided_at, note, int(approval_id), str(guild_id), now),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, ?, 'approval', ?, ?, ?)",
                (decided_at, decided_by, f"approval.{state}", str(approval_id), meta, str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def cancel_approval_request(
        self,
        approval_id: int,
        *,
        guild_id: str,
        cancelled_by: str,
        cancelled_at: str,
        reason: str | None,
        now: str,
    ) -> bool:
        """Conditionally cancel a pending or approved, unexpired request."""
        meta = json.dumps({"reason": reason} if reason else {})

        def _run(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE approval_requests SET state = 'cancelled', cancelled_by = ?, "
                "cancelled_at = ?, cancel_reason = ? WHERE id = ? AND guild_id = ? "
                "AND state IN ('pending','approved') AND expires_at > ?",
                (cancelled_by, cancelled_at, reason, int(approval_id), str(guild_id), now),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'approval.cancelled', 'approval', ?, ?, ?)",
                (cancelled_at, cancelled_by, str(approval_id), meta, str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def approval_requests_due(self, now: str) -> list[sqlite3.Row]:
        """Live requests whose expiry has passed."""
        return await self.db.fetchall(
            "SELECT id, guild_id, state, expires_at FROM approval_requests "
            "WHERE state IN ('pending','approved') AND expires_at <= ? ORDER BY id",
            (now,),
        )

    async def expire_approval_request(
        self, approval_id: int, *, guild_id: str, now: str
    ) -> bool:
        """Conditionally expire one live request and record the audit event."""
        def _run(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE approval_requests SET state = 'expired' WHERE id = ? AND guild_id = ? "
                "AND state IN ('pending','approved') AND expires_at <= ?",
                (int(approval_id), str(guild_id), now),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, guild_id) "
                "VALUES (?, 'system', 'approval.expired', 'approval', ?, ?)",
                (now, str(approval_id), str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def get_approval_execution_by_key(
        self, idempotency_key: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM approval_executions WHERE idempotency_key = ?",
            (idempotency_key,),
        )

    async def get_approval_execution(
        self, execution_id: int, *, guild_id: str | None = None
    ) -> sqlite3.Row | None:
        if guild_id is None:
            return await self.db.fetchone(
                "SELECT * FROM approval_executions WHERE id = ?", (int(execution_id),)
            )
        return await self.db.fetchone(
            "SELECT e.* FROM approval_executions e "
            "JOIN approval_requests r ON r.id = e.approval_id "
            "WHERE e.id = ? AND r.guild_id = ?",
            (int(execution_id), str(guild_id)),
        )

    async def get_approval_execution_for_approval(
        self, approval_id: int, *, guild_id: str | None = None
    ) -> sqlite3.Row | None:
        if guild_id is None:
            return await self.db.fetchone(
                "SELECT * FROM approval_executions WHERE approval_id = ?", (int(approval_id),)
            )
        return await self.db.fetchone(
            "SELECT e.* FROM approval_executions e "
            "JOIN approval_requests r ON r.id = e.approval_id "
            "WHERE e.approval_id = ? AND r.guild_id = ?",
            (int(approval_id), str(guild_id)),
        )

    async def claim_approval_execution(
        self,
        approval_id: int,
        *,
        guild_id: str,
        executor_id: str,
        idempotency_key: str,
        now: str,
        action_type: str,
        target_type: str,
        target_id: str,
        content_version: str,
        content_hash: str,
    ) -> int | None:
        """Atomically revalidate the approval and claim one execution attempt.

        The conditional update binds the claim to the exact action, target,
        content version and content hash. A mismatch or a concurrent claim
        returns ``None`` without creating an execution row.
        """

        def _run(conn: sqlite3.Connection) -> int | None:
            cursor = conn.execute(
                "UPDATE approval_requests SET state = 'executing' WHERE id = ? AND guild_id = ? "
                "AND state = 'approved' AND expires_at > ? AND action_type = ? "
                "AND target_type = ? AND target_id = ? AND content_version = ? "
                "AND content_hash = ?",
                (
                    int(approval_id),
                    str(guild_id),
                    now,
                    action_type,
                    target_type,
                    target_id,
                    content_version,
                    content_hash,
                ),
            )
            if cursor.rowcount != 1:
                return None
            execution = conn.execute(
                "INSERT INTO approval_executions (approval_id, idempotency_key, executor_id, "
                "state, started_at) VALUES (?, ?, ?, 'executing', ?)",
                (int(approval_id), idempotency_key, executor_id, now),
            )
            execution_id = int(execution.lastrowid)
            meta = json.dumps(
                {
                    "execution_id": execution_id,
                    "idempotency_key": idempotency_key,
                    "action_type": action_type,
                    "target_type": target_type,
                    "target_id": target_id,
                    "content_version": content_version,
                    "content_hash": content_hash,
                }
            )
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'approval.execution_started', 'approval', ?, ?, ?)",
                (now, executor_id, str(approval_id), meta, str(guild_id)),
            )
            return execution_id

        return await self.db.run_in_transaction_with_result(_run)

    async def finish_approval_execution(
        self,
        execution_id: int,
        *,
        approval_id: int,
        guild_id: str,
        executor_id: str,
        outcome: str,
        finished_at: str,
        error_code: str | None,
        result_ref: str | None = None,
    ) -> bool:
        """Conditionally close an executing attempt and its parent request."""
        audit_action = (
            "approval.executed" if outcome == "executed" else "approval.execution_failed"
        )
        meta: dict[str, Any] = {
            "execution_id": int(execution_id),
            "outcome": outcome,
            "error_code": error_code,
        }
        if result_ref is not None:
            meta["result_ref"] = result_ref
        meta_json = json.dumps(meta)

        def _run(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE approval_executions SET state = ?, finished_at = ?, error_code = ?, "
                "result_ref = ? WHERE id = ? AND approval_id = ? AND executor_id = ? "
                "AND state = 'executing'",
                (
                    outcome,
                    finished_at,
                    error_code,
                    result_ref,
                    int(execution_id),
                    int(approval_id),
                    executor_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            request = conn.execute(
                "UPDATE approval_requests SET state = ? WHERE id = ? AND guild_id = ? "
                "AND state = 'executing'",
                (outcome, int(approval_id), str(guild_id)),
            )
            if request.rowcount != 1:
                raise sqlite3.IntegrityError("approval and execution states disagree")
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, ?, 'approval', ?, ?, ?)",
                (finished_at, executor_id, audit_action, str(approval_id), meta_json, str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def reconcile_approval_execution(
        self,
        execution_id: int,
        *,
        approval_id: int,
        guild_id: str,
        actor_id: str,
        finished_at: str,
        result_ref: str,
    ) -> bool:
        """Record proven success for a stuck claim, regardless of original executor.

        Only the approval service may reach this after an authorized human supplies
        evidence that Discord already accepted the message. It never resets a claim.
        """
        meta = json.dumps(
            {
                "execution_id": int(execution_id),
                "outcome": "executed",
                "result_ref": result_ref,
                "reconciled_by": actor_id,
            }
        )

        def _run(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE approval_executions SET state = 'executed', finished_at = ?, "
                "error_code = NULL, result_ref = ? WHERE id = ? AND approval_id = ? "
                "AND state = 'executing'",
                (
                    finished_at,
                    result_ref,
                    int(execution_id),
                    int(approval_id),
                ),
            )
            if cursor.rowcount != 1:
                return False
            request = conn.execute(
                "UPDATE approval_requests SET state = 'executed' WHERE id = ? AND guild_id = ? "
                "AND state = 'executing'",
                (int(approval_id), str(guild_id)),
            )
            if request.rowcount != 1:
                raise sqlite3.IntegrityError("approval and execution states disagree")
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'approval.executed_reconciled', 'approval', ?, ?, ?)",
                (finished_at, actor_id, str(approval_id), meta, str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    # ------------------------------------------------------------ content studio

    async def insert_content_draft_with_version(
        self,
        *,
        guild_id: str,
        target_channel_id: str,
        content_type: str,
        topic: str,
        audience: str,
        tone: str,
        purpose: str,
        created_by: str,
        created_at: str,
        content: str,
        content_hash: str,
        validation_json: str,
        source_kind: str = "manual",
        source_ref: str | None = None,
    ) -> int:
        """Create draft row, immutable version 1, and the creation audit atomically."""
        meta = json.dumps(
            {
                "guild_id": str(guild_id),
                "target_channel_id": str(target_channel_id),
                "content_type": content_type,
                "version": 1,
                "content_hash": content_hash,
                "source_kind": source_kind,
                "source_ref": source_ref,
            }
        )

        def _run(conn: sqlite3.Connection) -> int:
            if source_ref is not None:
                existing = conn.execute(
                    "SELECT id FROM content_drafts WHERE guild_id = ? AND source_kind = ? "
                    "AND source_ref = ? ORDER BY id ASC LIMIT 1",
                    (str(guild_id), source_kind, source_ref),
                ).fetchone()
                if existing is not None:
                    # Idempotent retry: the scheduled source already produced a
                    # draft, so reuse it instead of creating a duplicate. The
                    # single serialized connection makes this check-and-insert
                    # atomic with respect to other writers.
                    return int(existing["id"])
            draft = conn.execute(
                "INSERT INTO content_drafts (guild_id, target_channel_id, content_type, topic, "
                "audience, tone, purpose, created_by, created_at, updated_at, current_version, "
                "state, source_kind, source_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'draft', ?, ?)",
                (
                    str(guild_id),
                    str(target_channel_id),
                    content_type,
                    topic,
                    audience,
                    tone,
                    purpose,
                    created_by,
                    created_at,
                    created_at,
                    source_kind,
                    source_ref,
                ),
            )
            draft_id = int(draft.lastrowid)
            conn.execute(
                "INSERT INTO content_draft_versions (draft_id, version, content, content_hash, "
                "validation_json, created_by, created_at) VALUES (?, 1, ?, ?, ?, ?, ?)",
                (draft_id, content, content_hash, validation_json, created_by, created_at),
            )
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'content.draft_created', 'content_draft', ?, ?, ?)",
                (created_at, created_by, str(draft_id), meta, str(guild_id)),
            )
            return draft_id

        return await self.db.run_in_transaction_with_result(_run)

    async def get_content_draft_by_source(
        self, *, guild_id: str, source_kind: str, source_ref: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM content_drafts WHERE guild_id = ? AND source_kind = ? "
            "AND source_ref = ? ORDER BY id ASC LIMIT 1",
            (str(guild_id), source_kind, source_ref),
        )

    async def get_content_draft(self, draft_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM content_drafts WHERE id = ? AND guild_id = ?",
            (int(draft_id), str(guild_id)),
        )

    async def get_content_draft_version(
        self, draft_id: int, version: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT v.* FROM content_draft_versions v "
            "JOIN content_drafts d ON d.id = v.draft_id "
            "WHERE v.draft_id = ? AND v.version = ? AND d.guild_id = ?",
            (int(draft_id), int(version), str(guild_id)),
        )

    async def list_content_drafts(
        self, *, guild_id: str, status: str | None = None, limit: int = 25
    ) -> list[sqlite3.Row]:
        status_sql = (
            "CASE "
            "WHEN d.state = 'published' THEN 'published' "
            "WHEN a.id IS NULL THEN CASE WHEN d.state = 'failed' THEN 'failed' ELSE 'draft' END "
            "WHEN a.state = 'executed' THEN 'published' "
            "ELSE a.state END"
        )
        query = (
            "SELECT d.* FROM content_drafts d "
            "LEFT JOIN approval_requests a ON a.id = d.approval_id "
            f"WHERE d.guild_id = ?{f' AND {status_sql} = ?' if status else ''} "
            "ORDER BY d.updated_at DESC, d.id DESC LIMIT ?"
        )
        params: tuple = (str(guild_id), int(limit))
        if status:
            params = (str(guild_id), status, int(limit))
        return await self.db.fetchall(query, params)

    async def find_content_version_by_hash(
        self,
        *,
        guild_id: str,
        content_hash: str,
        exclude_draft_id: int | None = None,
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT v.draft_id, v.version FROM content_draft_versions v "
            "JOIN content_drafts d ON d.id = v.draft_id "
            "WHERE d.guild_id = ? AND v.content_hash = ? AND d.id <> ? "
            "ORDER BY v.id DESC LIMIT 1",
            (str(guild_id), content_hash, int(exclude_draft_id or 0)),
        )

    async def add_content_draft_version(
        self,
        draft_id: int,
        *,
        guild_id: str,
        expected_version: int,
        target_channel_id: str,
        created_by: str,
        created_at: str,
        content: str,
        content_hash: str,
        validation_json: str,
    ) -> bool:
        """Append an immutable version and advance the draft atomically.

        The draft must still be unapproved and at ``expected_version``. A
        concurrent edit therefore has exactly one winner and never leaves an
        orphan version row.
        """
        next_version = int(expected_version) + 1
        meta = json.dumps(
            {
                "guild_id": str(guild_id),
                "version": next_version,
                "target_channel_id": str(target_channel_id),
                "content_hash": content_hash,
            }
        )

        def _run(conn: sqlite3.Connection) -> bool:
            updated = conn.execute(
                "UPDATE content_drafts SET current_version = ?, target_channel_id = ?, "
                "updated_at = ?, state = 'draft', approval_id = NULL "
                "WHERE id = ? AND guild_id = ? AND current_version = ? AND state = 'draft' "
                "AND approval_id IS NULL",
                (
                    next_version,
                    str(target_channel_id),
                    created_at,
                    int(draft_id),
                    str(guild_id),
                    int(expected_version),
                ),
            )
            if updated.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO content_draft_versions (draft_id, version, content, content_hash, "
                "validation_json, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(draft_id),
                    next_version,
                    content,
                    content_hash,
                    validation_json,
                    created_by,
                    created_at,
                ),
            )
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'content.version_created', 'content_draft', ?, ?, ?)",
                (created_at, created_by, str(draft_id), meta, str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def set_content_draft_approval(
        self,
        draft_id: int,
        *,
        guild_id: str,
        expected_version: int,
        approval_id: int,
        submitted_by: str,
        updated_at: str,
    ) -> bool:
        """Attach one pending approval to the exact current draft version."""
        meta = json.dumps(
            {
                "guild_id": str(guild_id),
                "approval_id": int(approval_id),
                "version": int(expected_version),
            }
        )

        def _run(conn: sqlite3.Connection) -> bool:
            updated = conn.execute(
                "UPDATE content_drafts SET approval_id = ?, state = 'submitted', updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND current_version = ? AND state = 'draft' "
                "AND approval_id IS NULL",
                (
                    int(approval_id),
                    updated_at,
                    int(draft_id),
                    str(guild_id),
                    int(expected_version),
                ),
            )
            if updated.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
                "meta_json, guild_id) VALUES (?, ?, 'content.submitted', 'content_draft', ?, ?, ?)",
                (updated_at, submitted_by, str(draft_id), meta, str(guild_id)),
            )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def clear_content_draft_approval(
        self,
        draft_id: int,
        *,
        guild_id: str,
        expected_version: int,
        approval_id: int,
        updated_at: str,
    ) -> bool:
        """Detach a finished/cancelled approval before a new version is written."""
        return (
            await self.db.execute(
                "UPDATE content_drafts SET approval_id = NULL, state = 'draft', updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND current_version = ? AND approval_id = ?",
                (
                    updated_at,
                    int(draft_id),
                    str(guild_id),
                    int(expected_version),
                    int(approval_id),
                ),
            )
            == 1
        )

    async def set_content_draft_state(
        self,
        draft_id: int,
        *,
        guild_id: str,
        state: str,
        updated_at: str,
        expected_approval_id: int | None = None,
    ) -> bool:
        """Record a terminal publish result without ever overwriting 'published'."""
        return (
            await self.db.execute(
                "UPDATE content_drafts SET state = ?, updated_at = ? WHERE id = ? AND guild_id = ? "
                "AND state IN ('submitted','failed') AND (? IS NULL OR approval_id = ?)",
                (
                    state,
                    updated_at,
                    int(draft_id),
                    str(guild_id),
                    None if expected_approval_id is None else int(expected_approval_id),
                    None if expected_approval_id is None else int(expected_approval_id),
                ),
            )
            == 1
        )

    # ---------------------------------------------------------------- missions

    async def insert_mission(
        self,
        *,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        requester_id: str,
        goal: str,
        mode: str,
        trace_id: str,
        idempotency_key: str | None,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO missions (guild_id, channel_id, parent_channel_id, requester_id, goal, "
            "mode, status, plan_json, trace_id, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'planned', '[]', ?, ?, ?, ?)",
            (
                str(guild_id),
                str(channel_id),
                None if parent_channel_id is None else str(parent_channel_id),
                str(requester_id),
                goal,
                mode,
                trace_id,
                idempotency_key,
                created_at,
                created_at,
            ),
        )

    async def insert_mission_with_steps(
        self,
        *,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        requester_id: str,
        goal: str,
        mode: str,
        trace_id: str,
        idempotency_key: str | None,
        created_at: str,
        plan_json: str,
        steps: list[tuple[int, str, str, str]],
    ) -> int:
        """Create the mission and all planned steps in one transaction."""

        def _run(conn: sqlite3.Connection) -> int:
            mission = conn.execute(
                "INSERT INTO missions (guild_id, channel_id, parent_channel_id, requester_id, "
                "goal, mode, status, plan_json, trace_id, idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?)",
                (
                    str(guild_id),
                    str(channel_id),
                    None if parent_channel_id is None else str(parent_channel_id),
                    str(requester_id),
                    goal,
                    mode,
                    plan_json,
                    trace_id,
                    idempotency_key,
                    created_at,
                    created_at,
                ),
            )
            mission_id = int(mission.lastrowid)
            for step_index, title, skill_id, input_json in steps:
                conn.execute(
                    "INSERT INTO mission_steps (mission_id, step_index, title, skill_id, input_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (mission_id, int(step_index), title, skill_id, input_json),
                )
            return mission_id

        return await self.db.run_in_transaction_with_result(_run)

    async def find_mission_by_idempotency(
        self, *, guild_id: str, idempotency_key: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM missions WHERE guild_id = ? AND idempotency_key = ?",
            (str(guild_id), idempotency_key),
        )

    async def get_mission(self, mission_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM missions WHERE id = ? AND guild_id = ?",
            (int(mission_id), str(guild_id)),
        )

    async def list_missions(
        self, *, guild_id: str, status: str | None = None, limit: int = 20
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM missions WHERE guild_id = ?"
        params: list[Any] = [str(guild_id)]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return await self.db.fetchall(query, tuple(params))

    async def mission_counts(self, *, guild_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS c FROM missions WHERE guild_id = ? GROUP BY status",
            (str(guild_id),),
        )
        return {str(row["status"]): int(row["c"]) for row in rows}

    async def set_mission_plan(
        self, mission_id: int, *, guild_id: str, plan_json: str, updated_at: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE missions SET plan_json = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND status = 'planned'",
                (plan_json, updated_at, int(mission_id), str(guild_id)),
            )
            == 1
        )

    async def start_mission(self, mission_id: int, *, guild_id: str, started_at: str) -> bool:
        """Move a planned mission to running unless cancellation was already requested."""
        return (
            await self.db.execute(
                "UPDATE missions SET status = 'running', started_at = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND status = 'planned' AND cancel_requested_at IS NULL",
                (started_at, started_at, int(mission_id), str(guild_id)),
            )
            == 1
        )

    async def finish_mission(
        self,
        mission_id: int,
        *,
        guild_id: str,
        status: str,
        result_json: str | None,
        error_code: str | None,
        finished_at: str,
    ) -> bool:
        """Record a terminal status once; terminal missions are never overwritten."""
        return (
            await self.db.execute(
                "UPDATE missions SET status = ?, result_json = ?, error_code = ?, "
                "finished_at = ?, updated_at = ? WHERE id = ? AND guild_id = ? "
                "AND status IN ('planned','running')",
                (
                    status,
                    result_json,
                    error_code,
                    finished_at,
                    finished_at,
                    int(mission_id),
                    str(guild_id),
                ),
            )
            == 1
        )

    async def request_mission_cancel(
        self, mission_id: int, *, guild_id: str, requested_at: str
    ) -> bool:
        """Set the cooperative cancel flag; the runner observes it between steps."""
        return (
            await self.db.execute(
                "UPDATE missions SET cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND status IN ('planned','running') "
                "AND cancel_requested_at IS NULL",
                (requested_at, requested_at, int(mission_id), str(guild_id)),
            )
            == 1
        )

    async def mission_cancel_requested(self, mission_id: int, *, guild_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT cancel_requested_at FROM missions WHERE id = ? AND guild_id = ?",
            (int(mission_id), str(guild_id)),
        )
        return bool(row is not None and row["cancel_requested_at"])

    async def add_mission_step(
        self,
        mission_id: int,
        *,
        step_index: int,
        title: str,
        skill_id: str,
        input_json: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO mission_steps (mission_id, step_index, title, skill_id, input_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(mission_id), int(step_index), title, skill_id, input_json),
        )

    async def start_mission_step(self, step_id: int, *, mission_id: int, started_at: str) -> bool:
        return (
            await self.db.execute(
                "UPDATE mission_steps SET status = 'running', started_at = ? "
                "WHERE id = ? AND mission_id = ? AND status = 'pending'",
                (started_at, int(step_id), int(mission_id)),
            )
            == 1
        )

    async def finish_mission_step(
        self,
        step_id: int,
        *,
        mission_id: int,
        status: str,
        output_json: str | None,
        references_json: str,
        error: str | None,
        finished_at: str,
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE mission_steps SET status = ?, output_json = ?, references_json = ?, "
                "error = ?, finished_at = ? WHERE id = ? AND mission_id = ? "
                "AND status IN ('pending','running')",
                (
                    status,
                    output_json,
                    references_json,
                    error,
                    finished_at,
                    int(step_id),
                    int(mission_id),
                ),
            )
            == 1
        )

    async def list_mission_steps(self, mission_id: int, *, guild_id: str) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT s.* FROM mission_steps s JOIN missions m ON m.id = s.mission_id "
            "WHERE s.mission_id = ? AND m.guild_id = ? ORDER BY s.step_index",
            (int(mission_id), str(guild_id)),
        )

    async def close_open_mission_steps(
        self,
        mission_id: int,
        *,
        status: str,
        finished_at: str,
        error: str | None = None,
    ) -> int:
        """Close leftover pending/running steps after a failure or cancellation."""
        if status not in {"skipped", "cancelled"}:
            raise ValueError("Open mission steps may only be closed as skipped or cancelled.")
        return await self.db.execute(
            "UPDATE mission_steps SET status = ?, finished_at = ?, error = ? "
            "WHERE mission_id = ? AND status IN ('pending','running')",
            (status, finished_at, error, int(mission_id)),
        )

    async def fail_interrupted_missions(self, *, finished_at: str) -> int:
        """Close missions left running by a restart; their steps become cancelled.

        Called once at startup. No guild filter: this is internal recovery, not a
        user-facing read, and it only moves non-terminal rows to a terminal state.
        """

        def _run(conn: sqlite3.Connection) -> int:
            # The caller-invoked transaction wrapper owns BEGIN/COMMIT; issuing
            # another BEGIN here would fail with "transaction within a transaction".
            conn.execute(
                "UPDATE mission_steps SET status = 'cancelled', finished_at = ?, "
                "error = 'interrupted by restart' "
                "WHERE status IN ('pending','running') AND mission_id IN "
                "(SELECT id FROM missions WHERE status IN ('planned','running'))",
                (finished_at,),
            )
            updated = conn.execute(
                "UPDATE missions SET status = 'failed', error_code = 'interrupted', "
                "finished_at = ?, updated_at = ? WHERE status IN ('planned','running')",
                (finished_at, finished_at),
            ).rowcount
            return int(updated or 0)

        return await self.db.run_in_transaction_with_result(_run)

    # ---------------------------------------------------------------- research

    async def insert_research_report(
        self,
        *,
        guild_id: str,
        mission_id: int | None,
        query: str,
        provider: str,
        status: str,
        summary: str,
        synthesis: str,
        sources_json: str,
        error_code: str | None,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO research_reports (guild_id, mission_id, query, provider, status, "
            "summary, synthesis, sources_json, error_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(guild_id),
                None if mission_id is None else int(mission_id),
                query,
                provider,
                status,
                summary,
                synthesis,
                sources_json,
                error_code,
                created_at,
            ),
        )

    async def get_research_report(self, report_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM research_reports WHERE id = ? AND guild_id = ?",
            (int(report_id), str(guild_id)),
        )

    async def list_research_reports(self, *, guild_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT * FROM research_reports WHERE guild_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(guild_id), int(limit)),
        )

    # -------------------------------------------------------- content planning

    async def insert_event_plan(
        self,
        *,
        guild_id: str,
        title: str,
        brief: str,
        scheduled_for: str | None,
        target_channel_id: str | None,
        plan_json: str,
        created_by: str,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO event_plans (guild_id, title, brief, scheduled_for, target_channel_id, "
            "plan_json, state, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
            (
                str(guild_id),
                title,
                brief,
                scheduled_for,
                None if target_channel_id is None else str(target_channel_id),
                plan_json,
                str(created_by),
                created_at,
                created_at,
            ),
        )

    async def get_event_plan(self, plan_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM event_plans WHERE id = ? AND guild_id = ?",
            (int(plan_id), str(guild_id)),
        )

    async def list_event_plans(self, *, guild_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT * FROM event_plans WHERE guild_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(guild_id), int(limit)),
        )

    async def set_event_plan_state(
        self, plan_id: int, *, guild_id: str, state: str, updated_at: str
    ) -> bool:
        # Archiving is the only transition and is allowed once, from draft.
        return (
            await self.db.execute(
                "UPDATE event_plans SET state = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND state = 'draft'",
                (state, updated_at, int(plan_id), str(guild_id)),
            )
            == 1
        )

    async def insert_calendar_entry(
        self,
        *,
        guild_id: str,
        title: str,
        kind: str,
        starts_at: str,
        ends_at: str | None,
        target_channel_id: str | None,
        draft_id: int | None,
        event_plan_id: int | None,
        notes: str,
        created_by: str,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO content_calendar_entries (guild_id, title, kind, starts_at, ends_at, "
            "target_channel_id, draft_id, event_plan_id, notes, state, created_by, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)",
            (
                str(guild_id),
                title,
                kind,
                starts_at,
                ends_at,
                None if target_channel_id is None else str(target_channel_id),
                None if draft_id is None else int(draft_id),
                None if event_plan_id is None else int(event_plan_id),
                notes,
                str(created_by),
                created_at,
                created_at,
            ),
        )

    async def list_calendar_entries(
        self, *, guild_id: str, include_cancelled: bool = False, limit: int = 50
    ) -> list[sqlite3.Row]:
        query = (
            "SELECT * FROM content_calendar_entries WHERE guild_id = ?"
            + ("" if include_cancelled else " AND state <> 'cancelled'")
            + " ORDER BY starts_at, id LIMIT ?"
        )
        return await self.db.fetchall(query, (str(guild_id), int(limit)))

    async def set_calendar_entry_state(
        self, entry_id: int, *, guild_id: str, state: str, updated_at: str
    ) -> bool:
        # Cancellation is the only transition and is allowed once, from planned.
        return (
            await self.db.execute(
                "UPDATE content_calendar_entries SET state = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND state = 'planned'",
                (state, updated_at, int(entry_id), str(guild_id)),
            )
            == 1
        )

    async def insert_contextual_reply(
        self,
        *,
        guild_id: str,
        channel_id: str,
        source_message_id: str,
        source_excerpt: str,
        tone: str,
        purpose: str,
        content: str,
        created_by: str,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO contextual_replies (guild_id, channel_id, source_message_id, "
            "source_excerpt, tone, purpose, content, state, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (
                str(guild_id),
                str(channel_id),
                str(source_message_id),
                source_excerpt,
                tone,
                purpose,
                content,
                str(created_by),
                created_at,
            ),
        )

    async def get_contextual_reply(self, reply_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM contextual_replies WHERE id = ? AND guild_id = ?",
            (int(reply_id), str(guild_id)),
        )

    async def list_contextual_replies(
        self, *, guild_id: str, limit: int = 20
    ) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT * FROM contextual_replies WHERE guild_id = ? AND state = 'draft' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(guild_id), int(limit)),
        )

    async def dismiss_contextual_reply(self, reply_id: int, *, guild_id: str) -> bool:
        return (
            await self.db.execute(
                "UPDATE contextual_replies SET state = 'dismissed' "
                "WHERE id = ? AND guild_id = ? AND state = 'draft'",
                (int(reply_id), str(guild_id)),
            )
            == 1
        )

    # ------------------------------------------------------------- evaluations

    async def insert_agent_eval_run(
        self,
        *,
        guild_id: str,
        requested_by: str,
        status: str,
        report_json: str,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO agent_eval_runs (guild_id, requested_by, status, report_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(guild_id), str(requested_by), status, report_json, created_at),
        )

    async def latest_agent_eval_run(self, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM agent_eval_runs WHERE guild_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (str(guild_id),),
        )

    async def recent_audit(self, *, guild_id: str, limit: int = 20) -> list[sqlite3.Row]:
        """Recent audit rows for one guild.

        Older rows predate the guild_id column, so their meta JSON is the fallback;
        anything without a guild marker is never returned to a guild-scoped view.
        """
        return await self.db.fetchall(
            "SELECT * FROM audit_log WHERE guild_id = ? "
            "OR (guild_id = '' AND json_extract(meta_json, '$.guild_id') = ?) "
            "ORDER BY id DESC LIMIT ?",
            (str(guild_id), str(guild_id), int(limit)),
        )

    # ------------------------------------------------------------- attachments

    async def insert_attachment(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        message_id: str,
        filename: str,
        kind: str,
        mime: str,
        size_bytes: int,
        sha256: str,
        width: int | None,
        height: int | None,
        pages: int | None,
        extracted_text: str,
        truncated: bool,
        error_code: str | None,
        created_at: str,
    ) -> int:
        """Store bounded attachment metadata and extracted text (never raw bytes)."""
        return await self.db.insert(
            "INSERT INTO attachments (guild_id, channel_id, user_id, message_id, filename, "
            "kind, mime, size_bytes, sha256, width, height, pages, extracted_text, "
            "truncated, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(guild_id),
                str(channel_id),
                str(user_id),
                str(message_id),
                filename,
                kind,
                mime,
                int(size_bytes),
                sha256,
                width,
                height,
                pages,
                extracted_text,
                1 if truncated else 0,
                error_code,
                created_at,
            ),
        )

    async def recent_attachments(
        self, *, guild_id: str, channel_id: str, user_id: str, limit: int = 6
    ) -> list[sqlite3.Row]:
        """Newest attachment rows for one conversation scope, oldest-first."""
        rows = await self.db.fetchall(
            "SELECT * FROM attachments WHERE guild_id = ? AND channel_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (str(guild_id), str(channel_id), str(user_id), int(limit)),
        )
        rows.reverse()
        return rows

    async def delete_attachments_for_scope(
        self, *, guild_id: str, channel_id: str, user_id: str
    ) -> int:
        return await self.db.execute(
            "DELETE FROM attachments WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (str(guild_id), str(channel_id), str(user_id)),
        )

    async def delete_attachments_for_channel(
        self, *, guild_id: str, channel_id: str
    ) -> int:
        """Delete attachment rows for a thread/channel (moderator purge)."""
        return await self.db.execute(
            "DELETE FROM attachments WHERE guild_id = ? AND channel_id = ?",
            (str(guild_id), str(channel_id)),
        )

    async def purge_expired_attachments(self, *, before: str) -> int:
        """Retention sweep: remove attachment metadata/extracts older than a cutoff."""
        return await self.db.execute(
            "DELETE FROM attachments WHERE created_at < ?", (str(before),)
        )

    # ----------------------------------------------------------- voice profiles

    async def get_voice_profile(self, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM guild_voice_profiles WHERE guild_id = ?", (str(guild_id),)
        )

    async def upsert_voice_profile(
        self,
        *,
        guild_id: str,
        tone: str,
        audience: str,
        emoji_policy: str,
        formatting: str,
        style_notes: str,
        banned_phrases_json: str,
        updated_by: str,
        now: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO guild_voice_profiles (guild_id, tone, audience, emoji_policy, formatting, "
            "style_notes, banned_phrases_json, updated_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET tone=excluded.tone, audience=excluded.audience, "
            "emoji_policy=excluded.emoji_policy, formatting=excluded.formatting, "
            "style_notes=excluded.style_notes, banned_phrases_json=excluded.banned_phrases_json, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (
                str(guild_id),
                tone,
                audience,
                emoji_policy,
                formatting,
                style_notes,
                banned_phrases_json,
                str(updated_by),
                now,
                now,
            ),
        )

    async def add_content_feedback(
        self,
        *,
        guild_id: str,
        draft_id: int | None,
        version: int | None,
        feedback: str,
        tags_json: str,
        created_by: str,
        created_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO content_feedback (guild_id, draft_id, version, feedback, tags_json, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(guild_id), draft_id, version, feedback, tags_json, str(created_by), created_at),
        )

    async def list_content_feedback(
        self, *, guild_id: str, limit: int = 8
    ) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT * FROM content_feedback WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (str(guild_id), int(limit)),
        )

    async def recent_approved_content(
        self, *, guild_id: str, limit: int = 40
    ) -> list[sqlite3.Row]:
        """Recent approved/submitted draft versions, newest first, for repetition checks."""
        return await self.db.fetchall(
            "SELECT v.id, v.draft_id, v.version, v.content, v.content_hash, d.state, d.updated_at "
            "FROM content_draft_versions v JOIN content_drafts d ON d.id = v.draft_id "
            "WHERE d.guild_id = ? AND d.state IN ('submitted','published') "
            "ORDER BY d.updated_at DESC, v.version DESC LIMIT ?",
            (str(guild_id), int(limit)),
        )

    # ------------------------------------------------------------- moderation

    async def get_moderation_policy(self, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM moderation_policies WHERE guild_id = ?", (str(guild_id),)
        )

    async def upsert_moderation_policy(
        self,
        *,
        guild_id: str,
        enabled: bool,
        report_channel_id: str | None,
        delete_enabled: bool,
        severity_threshold: str,
        confidence_threshold: float,
        categories_json: str,
        max_incidents_per_day: int,
        notify_user: bool,
        created_by: str,
        now: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO moderation_policies (guild_id, enabled, report_channel_id, delete_enabled, "
            "severity_threshold, confidence_threshold, categories_json, max_incidents_per_day, "
            "notify_user, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled, "
            "report_channel_id=excluded.report_channel_id, delete_enabled=excluded.delete_enabled, "
            "severity_threshold=excluded.severity_threshold, "
            "confidence_threshold=excluded.confidence_threshold, "
            "categories_json=excluded.categories_json, "
            "max_incidents_per_day=excluded.max_incidents_per_day, "
            "notify_user=excluded.notify_user, updated_at=excluded.updated_at",
            (
                str(guild_id),
                1 if enabled else 0,
                None if report_channel_id is None else str(report_channel_id),
                1 if delete_enabled else 0,
                severity_threshold,
                float(confidence_threshold),
                categories_json,
                int(max_incidents_per_day),
                1 if notify_user else 0,
                str(created_by),
                now,
                now,
            ),
        )

    async def insert_moderation_incident(
        self,
        *,
        guild_id: str,
        message_id: str,
        channel_id: str,
        author_id: str,
        category: str,
        severity: str,
        confidence: float,
        action_taken: str,
        repeat_count: int,
        recommended_next_step: str,
        created_at: str,
    ) -> tuple[int, bool]:
        """Insert one incident per message; returns ``(id, created)``.

        The unique ``(guild_id, message_id)`` key makes retries and duplicate
        event delivery harmless: the second call returns the existing id with
        ``created=False`` and the caller must not repeat a deletion/report.
        """

        def _run(conn: sqlite3.Connection) -> tuple[int, bool]:
            cursor = conn.execute(
                "INSERT INTO moderation_incidents (guild_id, message_id, channel_id, author_id, "
                "category, severity, confidence, action_taken, repeat_count, "
                "recommended_next_step, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, message_id) DO NOTHING",
                (
                    str(guild_id),
                    str(message_id),
                    str(channel_id),
                    str(author_id),
                    category,
                    severity,
                    float(confidence),
                    action_taken,
                    int(repeat_count),
                    recommended_next_step,
                    created_at,
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT id FROM moderation_incidents WHERE guild_id = ? AND message_id = ?",
                (str(guild_id), str(message_id)),
            ).fetchone()
            return int(row["id"]), cursor.rowcount == 1

        return await self.db.run_in_transaction_with_result(_run)

    async def get_moderation_incident(
        self, incident_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM moderation_incidents WHERE id = ? AND guild_id = ?",
            (int(incident_id), str(guild_id)),
        )

    async def get_moderation_incident_by_message(
        self, *, guild_id: str, message_id: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM moderation_incidents WHERE guild_id = ? AND message_id = ?",
            (str(guild_id), str(message_id)),
        )

    async def list_moderation_incidents(
        self, *, guild_id: str, state: str | None = None, limit: int = 20
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM moderation_incidents WHERE guild_id = ?"
        params: list[Any] = [str(guild_id)]
        if state:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return await self.db.fetchall(query, tuple(params))

    async def set_incident_action(
        self,
        incident_id: int,
        *,
        guild_id: str,
        action_taken: str,
        report_delivered: bool | None = None,
        updated_at: str,
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE moderation_incidents SET action_taken = ?, "
                "report_delivered = COALESCE(?, report_delivered), updated_at = ? "
                "WHERE id = ? AND guild_id = ?",
                (
                    action_taken,
                    None if report_delivered is None else (1 if report_delivered else 0),
                    updated_at,
                    int(incident_id),
                    str(guild_id),
                ),
            )
            == 1
        )

    async def review_moderation_incident(
        self,
        incident_id: int,
        *,
        guild_id: str,
        state: str,
        reviewer_id: str,
        note: str | None,
        updated_at: str,
    ) -> bool:
        """Human review is a single open -> confirmed/dismissed/overridden transition."""
        if state not in {"confirmed", "dismissed", "overridden"}:
            raise ValueError("invalid moderation review state")
        return (
            await self.db.execute(
                "UPDATE moderation_incidents SET state = ?, reviewer_id = ?, review_note = ?, "
                "updated_at = ? WHERE id = ? AND guild_id = ? AND state = 'open'",
                (state, str(reviewer_id), note, updated_at, int(incident_id), str(guild_id)),
            )
            == 1
        )

    async def count_author_incidents_since(
        self, *, guild_id: str, author_id: str, since: str, state: str | None = None
    ) -> int:
        sql = (
            "SELECT COUNT(*) AS c FROM moderation_incidents "
            "WHERE guild_id = ? AND author_id = ? AND created_at >= ?"
        )
        params: list[object] = [str(guild_id), str(author_id), since]
        if state is not None:
            sql += " AND state = ?"
            params.append(str(state))
        row = await self.db.fetchone(sql, tuple(params))
        return int(row["c"]) if row is not None else 0

    async def add_moderation_warning(
        self,
        *,
        guild_id: str,
        user_id: str,
        incident_id: int,
        category: str,
        created_at: str,
    ) -> bool:
        """Record at most one warning per incident; True when this call created it."""
        return (
            await self.db.execute(
                "INSERT INTO moderation_warnings (guild_id, user_id, incident_id, category, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, incident_id) DO NOTHING",
                (str(guild_id), str(user_id), int(incident_id), category, created_at),
            )
            == 1
        )

    async def count_moderation_warnings(self, *, guild_id: str, user_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM moderation_warnings WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        )
        return int(row["c"]) if row is not None else 0

    # -------------------------------------------------------------- host agent

    async def insert_agent_run_with_steps(
        self,
        *,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str | None,
        requester_id: str,
        goal: str,
        mode: str,
        trace_id: str,
        idempotency_key: str | None,
        created_at: str,
        plan_json: str,
        steps: list[tuple[int, str, str, str, str, str]],
        audit_meta: dict | None = None,
    ) -> int:
        """Create one host-agent run, its steps and its audit row atomically."""

        def _run(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO agent_runs (guild_id, channel_id, parent_channel_id, requester_id, "
                "goal, mode, status, plan_json, trace_id, idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?)",
                (
                    str(guild_id),
                    str(channel_id),
                    None if parent_channel_id is None else str(parent_channel_id),
                    str(requester_id),
                    goal,
                    mode,
                    plan_json,
                    trace_id,
                    idempotency_key,
                    created_at,
                    created_at,
                ),
            )
            run_id = int(cursor.lastrowid)
            for (
                step_index,
                role_id,
                title,
                skill_id,
                depends_on_json,
                input_json,
            ) in steps:
                conn.execute(
                    "INSERT INTO agent_run_steps (run_id, step_index, role_id, title, skill_id, "
                    "depends_on_json, input_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        int(step_index),
                        role_id,
                        title,
                        skill_id,
                        depends_on_json,
                        input_json,
                    ),
                )
            if audit_meta is not None:
                conn.execute(
                    "INSERT INTO audit_log (created_at, actor_id, action, target_type, "
                    "target_id, meta_json, guild_id) VALUES (?, ?, 'agent.created', "
                    "'agent_run', ?, ?, ?)",
                    (
                        created_at,
                        str(requester_id),
                        str(run_id),
                        json.dumps(audit_meta),
                        str(guild_id),
                    ),
                )
            return run_id

        return await self.db.run_in_transaction_with_result(_run)

    async def find_agent_run_by_idempotency(
        self, *, guild_id: str, idempotency_key: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM agent_runs WHERE guild_id = ? AND idempotency_key = ?",
            (str(guild_id), idempotency_key),
        )

    async def get_agent_run(self, run_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM agent_runs WHERE id = ? AND guild_id = ?",
            (int(run_id), str(guild_id)),
        )

    async def list_agent_runs(
        self, *, guild_id: str, status: str | None = None, limit: int = 20
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM agent_runs WHERE guild_id = ?"
        params: list[Any] = [str(guild_id)]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return await self.db.fetchall(query, tuple(params))

    async def agent_run_counts(self, *, guild_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS c FROM agent_runs WHERE guild_id = ? GROUP BY status",
            (str(guild_id),),
        )
        return {str(row["status"]): int(row["c"]) for row in rows}

    async def start_agent_run(
        self, run_id: int, *, guild_id: str, started_at: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE agent_runs SET status = 'running', started_at = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND status = 'planned' "
                "AND cancel_requested_at IS NULL",
                (started_at, started_at, int(run_id), str(guild_id)),
            )
            == 1
        )

    async def finish_agent_run(
        self,
        run_id: int,
        *,
        guild_id: str,
        status: str,
        result_json: str | None,
        error_code: str | None,
        finished_at: str,
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE agent_runs SET status = ?, result_json = ?, error_code = ?, "
                "finished_at = ?, updated_at = ? WHERE id = ? AND guild_id = ? "
                "AND status IN ('planned','running')",
                (
                    status,
                    result_json,
                    error_code,
                    finished_at,
                    finished_at,
                    int(run_id),
                    str(guild_id),
                ),
            )
            == 1
        )

    async def request_agent_run_cancel(
        self, run_id: int, *, guild_id: str, requested_at: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE agent_runs SET cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND status IN ('planned','running') "
                "AND cancel_requested_at IS NULL",
                (requested_at, requested_at, int(run_id), str(guild_id)),
            )
            == 1
        )

    async def agent_run_cancel_requested(self, run_id: int, *, guild_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT cancel_requested_at FROM agent_runs WHERE id = ? AND guild_id = ?",
            (int(run_id), str(guild_id)),
        )
        return bool(row is not None and row["cancel_requested_at"])

    async def start_agent_run_step(
        self, step_id: int, *, run_id: int, started_at: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE agent_run_steps SET status = 'running', started_at = ? "
                "WHERE id = ? AND run_id = ? AND status = 'pending'",
                (started_at, int(step_id), int(run_id)),
            )
            == 1
        )

    async def finish_agent_run_step(
        self,
        step_id: int,
        *,
        run_id: int,
        status: str,
        output_json: str | None,
        references_json: str,
        error: str | None,
        finished_at: str,
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE agent_run_steps SET status = ?, output_json = ?, references_json = ?, "
                "error = ?, finished_at = ? WHERE id = ? AND run_id = ? "
                "AND status IN ('pending','running')",
                (
                    status,
                    output_json,
                    references_json,
                    error,
                    finished_at,
                    int(step_id),
                    int(run_id),
                ),
            )
            == 1
        )

    async def list_agent_run_steps(
        self, run_id: int, *, guild_id: str
    ) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT s.* FROM agent_run_steps s JOIN agent_runs r ON r.id = s.run_id "
            "WHERE s.run_id = ? AND r.guild_id = ? ORDER BY s.step_index",
            (int(run_id), str(guild_id)),
        )

    async def close_open_agent_steps(
        self,
        run_id: int,
        *,
        status: str,
        finished_at: str,
        error: str | None = None,
    ) -> int:
        if status not in {"skipped", "cancelled"}:
            raise ValueError("Open agent steps may only be closed as skipped or cancelled.")
        return await self.db.execute(
            "UPDATE agent_run_steps SET status = ?, finished_at = ?, error = ? "
            "WHERE run_id = ? AND status IN ('pending','running')",
            (status, finished_at, error, int(run_id)),
        )

    async def fail_interrupted_agent_runs(self, *, finished_at: str) -> int:
        """Close runs left planned/running by a restart (startup recovery)."""

        def _run(conn: sqlite3.Connection) -> int:
            conn.execute(
                "UPDATE agent_run_steps SET status = 'cancelled', finished_at = ?, "
                "error = 'interrupted by restart' "
                "WHERE status IN ('pending','running') AND run_id IN "
                "(SELECT id FROM agent_runs WHERE status IN ('planned','running'))",
                (finished_at,),
            )
            updated = conn.execute(
                "UPDATE agent_runs SET status = 'failed', error_code = 'interrupted', "
                "finished_at = ?, updated_at = ? WHERE status IN ('planned','running')",
                (finished_at, finished_at),
            ).rowcount
            return int(updated or 0)

        return await self.db.run_in_transaction_with_result(_run)

    # -------------------------------------------------------------- scheduler

    async def insert_scheduled_job(
        self,
        *,
        guild_id: str,
        job_kind: str,
        payload_json: str,
        run_at: str,
        timezone: str,
        max_attempts: int,
        idempotency_key: str,
        created_by: str,
        now: str,
    ) -> tuple[int, bool]:
        """Insert a durable job; a repeated guild+key returns the existing id."""

        def _run(conn: sqlite3.Connection) -> tuple[int, bool]:
            cursor = conn.execute(
                "INSERT INTO scheduled_jobs (guild_id, job_kind, payload_json, run_at, timezone, "
                "status, attempts, max_attempts, idempotency_key, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, idempotency_key) DO NOTHING",
                (
                    str(guild_id),
                    job_kind,
                    payload_json,
                    run_at,
                    timezone,
                    int(max_attempts),
                    idempotency_key,
                    str(created_by),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM scheduled_jobs WHERE guild_id = ? AND idempotency_key = ?",
                (str(guild_id), idempotency_key),
            ).fetchone()
            return int(row["id"]), cursor.rowcount == 1

        return await self.db.run_in_transaction_with_result(_run)

    async def get_scheduled_job(
        self, job_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM scheduled_jobs WHERE id = ? AND guild_id = ?",
            (int(job_id), str(guild_id)),
        )

    async def list_scheduled_jobs(
        self, *, guild_id: str, status: str | None = None, limit: int = 20
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM scheduled_jobs WHERE guild_id = ?"
        params: list[Any] = [str(guild_id)]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY run_at ASC, id ASC LIMIT ?"
        params.append(int(limit))
        return await self.db.fetchall(query, tuple(params))

    async def due_scheduled_jobs(self, now: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """Cross-guild internal runner read; never exposed to a guild-facing view."""
        return await self.db.fetchall(
            "SELECT * FROM scheduled_jobs WHERE status = 'pending' AND run_at <= ? "
            "ORDER BY run_at ASC, id ASC LIMIT ?",
            (now, int(limit)),
        )

    async def claim_scheduled_job(self, job_id: int, *, now: str) -> bool:
        return (
            await self.db.execute(
                "UPDATE scheduled_jobs SET status = 'running', attempts = attempts + 1, "
                "started_at = ?, finished_at = NULL, updated_at = ? WHERE id = ? "
                "AND status = 'pending' AND run_at <= ? AND attempts < max_attempts",
                (now, now, int(job_id), now),
            )
            == 1
        )

    async def finish_scheduled_job(
        self,
        job_id: int,
        *,
        status: str,
        last_error: str | None,
        finished_at: str,
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE scheduled_jobs SET status = ?, last_error = ?, finished_at = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (status, last_error, finished_at, finished_at, int(job_id)),
            )
            == 1
        )

    async def reschedule_scheduled_job(
        self, job_id: int, *, run_at: str, last_error: str, now: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE scheduled_jobs SET status = 'pending', run_at = ?, last_error = ?, "
                "updated_at = ?, started_at = NULL, finished_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (run_at, last_error, now, int(job_id)),
            )
            == 1
        )

    async def cancel_scheduled_job(
        self, job_id: int, *, guild_id: str, now: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE scheduled_jobs SET status = 'cancelled', finished_at = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND status IN ('pending','running')",
                (now, now, int(job_id), str(guild_id)),
            )
            == 1
        )

    async def recover_interrupted_scheduled_jobs(self, *, now: str) -> int:
        """Return interrupted jobs to pending, failing those out of attempts.

        A restart must not grant an extra attempt: rows at the attempt cap are
        marked failed instead of being requeued. Stale execution timestamps are
        cleared so a recovered row never looks like it is still running.
        """

        def _run(conn: sqlite3.Connection) -> int:
            exhausted = conn.execute(
                "SELECT id, guild_id, job_kind, attempts FROM scheduled_jobs "
                "WHERE status = 'running' AND attempts >= max_attempts"
            ).fetchall()
            recovered = conn.execute(
                "UPDATE scheduled_jobs SET status = 'pending', "
                "last_error = 'interrupted by restart', started_at = NULL, "
                "finished_at = NULL, updated_at = ? "
                "WHERE status = 'running' AND attempts < max_attempts",
                (now,),
            ).rowcount
            if exhausted:
                conn.execute(
                    "UPDATE scheduled_jobs SET status = 'failed', "
                    "last_error = 'interrupted by restart', finished_at = ?, updated_at = ? "
                    "WHERE status = 'running' AND attempts >= max_attempts",
                    (now, now),
                )
                for row in exhausted:
                    guild_id = str(row["guild_id"])
                    conn.execute(
                        "INSERT INTO audit_log (created_at, actor_id, action, target_type, "
                        "target_id, meta_json, guild_id) VALUES (?, 'scheduler', "
                        "'scheduler.job_failed', 'scheduled_job', ?, ?, ?)",
                        (
                            now,
                            str(int(row["id"])),
                            json.dumps(
                                {
                                    "guild_id": guild_id,
                                    "kind": str(row["job_kind"]),
                                    "attempts": int(row["attempts"]),
                                    "error_code": "interrupted by restart",
                                }
                            ),
                            guild_id,
                        ),
                    )
            return int(recovered or 0)

        return await self.db.run_in_transaction_with_result(_run)

    async def get_calendar_entry(
        self, entry_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM content_calendar_entries WHERE id = ? AND guild_id = ?",
            (int(entry_id), str(guild_id)),
        )

    async def link_calendar_entry_job(
        self, entry_id: int, *, guild_id: str, job_id: int, updated_at: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE content_calendar_entries SET scheduled_job_id = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ?",
                (int(job_id), updated_at, int(entry_id), str(guild_id)),
            )
            == 1
        )

    async def scheduled_jobs_for_calendar_entries(
        self, *, guild_id: str, entry_ids: list[int]
    ) -> dict[int, sqlite3.Row]:
        if not entry_ids:
            return {}
        placeholders = ",".join("?" for _ in entry_ids)
        rows = await self.db.fetchall(
            "SELECT e.id AS entry_id, j.* FROM content_calendar_entries e "
            "JOIN scheduled_jobs j ON j.id = e.scheduled_job_id AND j.guild_id = e.guild_id "
            f"WHERE e.guild_id = ? AND e.id IN ({placeholders})",
            (str(guild_id), *[int(entry_id) for entry_id in entry_ids]),
        )
        return {int(row["entry_id"]): row for row in rows}

    # ---------------------------------------------------------- reply approvals

    async def insert_reply_approval(
        self,
        *,
        guild_id: str,
        channel_id: str,
        reply_to_message_id: str | None,
        content: str,
        content_hash: str,
        requested_by: str,
        created_at: str,
        expires_at: str,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO reply_approvals (guild_id, channel_id, reply_to_message_id, content, "
            "content_hash, state, requested_by, created_at, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (
                str(guild_id),
                str(channel_id),
                None if reply_to_message_id is None else str(reply_to_message_id),
                content,
                content_hash,
                str(requested_by),
                created_at,
                expires_at,
                created_at,
            ),
        )

    async def get_reply_approval(
        self, reply_id: int, *, guild_id: str
    ) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM reply_approvals WHERE id = ? AND guild_id = ?",
            (int(reply_id), str(guild_id)),
        )

    async def list_reply_approvals(
        self, *, guild_id: str, state: str = "pending", limit: int = 10
    ) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT * FROM reply_approvals WHERE guild_id = ? AND state = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(guild_id), state, int(limit)),
        )

    async def decide_reply_approval(
        self,
        reply_id: int,
        *,
        guild_id: str,
        state: str,
        decided_by: str,
        decided_at: str,
        note: str | None = None,
        audit_meta: dict | None = None,
    ) -> bool:
        if state not in {"approved", "rejected"}:
            raise ValueError("invalid reply approval decision")

        def _run(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE reply_approvals SET state = ?, decided_by = ?, decided_at = ?, "
                "decision_note = ?, updated_at = ? WHERE id = ? AND guild_id = ? AND state = 'pending' "
                "AND expires_at > ?",
                (
                    state,
                    str(decided_by),
                    decided_at,
                    None if note is None else str(note)[:500],
                    decided_at,
                    int(reply_id),
                    str(guild_id),
                    decided_at,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if audit_meta is not None:
                conn.execute(
                    "INSERT INTO audit_log (created_at, actor_id, action, target_type, "
                    "target_id, meta_json, guild_id) VALUES (?, ?, ?, 'reply_approval', ?, ?, ?)",
                    (
                        decided_at,
                        str(decided_by),
                        f"chat.reply_approval_{state}",
                        str(reply_id),
                        json.dumps(audit_meta),
                        str(guild_id),
                    ),
                )
            return True

        return await self.db.run_in_transaction_with_result(_run)

    async def mark_reply_approval_sent(
        self, reply_id: int, *, guild_id: str, message_id: str, now: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE reply_approvals SET state = 'sent', sent_message_id = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND state = 'approved'",
                (str(message_id), now, int(reply_id), str(guild_id)),
            )
            == 1
        )

    async def mark_reply_approval_failed(
        self, reply_id: int, *, guild_id: str, error_code: str, now: str
    ) -> bool:
        return (
            await self.db.execute(
                "UPDATE reply_approvals SET state = 'failed', error_code = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ? AND state = 'approved'",
                (error_code, now, int(reply_id), str(guild_id)),
            )
            == 1
        )

    async def expire_reply_approvals(self, *, now: str) -> int:
        return await self.db.execute(
            "UPDATE reply_approvals SET state = 'expired', updated_at = ? "
            "WHERE state = 'pending' AND expires_at <= ?",
            (now, now),
        )
