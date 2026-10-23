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
        meta = json.dumps({"state": "approved"})

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
                "meta_json) VALUES (?, ?, 'solution.approved', 'solution', ?, ?)",
                (now, actor_id, str(solution_id), meta),
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
        self, text: str, *, guild_id: str, limit: int = 5, approved_only: bool = False
    ) -> list[sqlite3.Row]:
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
    ) -> list[sqlite3.Row]:
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
            "AND (t.discord_thread_id IS NULL OR t.discord_thread_id <> ?) "
            "ORDER BY score LIMIT ?",
            (match, str(guild_id), exclude_discord_thread_id or "", int(limit)),
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
                "meta_json) VALUES (?, ?, 'knowledge.document_purged', 'kb_doc', ?, ?)",
                (
                    now,
                    actor_id,
                    str(kb_doc_id),
                    json.dumps({"guild_id": str(guild_id), **facts}),
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
    ) -> int:
        return await self.db.insert(
            "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), actor_id, action, target_type, target_id, json.dumps(meta or {})),
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
                "meta_json) VALUES (?, ?, 'approval.requested', 'approval', ?, ?)",
                (created_at, requested_by, str(approval_id), meta),
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
                "meta_json) VALUES (?, ?, ?, 'approval', ?, ?)",
                (decided_at, decided_by, f"approval.{state}", str(approval_id), meta),
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
                "meta_json) VALUES (?, ?, 'approval.cancelled', 'approval', ?, ?)",
                (cancelled_at, cancelled_by, str(approval_id), meta),
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
                "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id) "
                "VALUES (?, 'system', 'approval.expired', 'approval', ?)",
                (now, str(approval_id)),
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

    async def get_approval_execution(self, execution_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM approval_executions WHERE id = ?", (int(execution_id),)
        )

    async def get_approval_execution_for_approval(self, approval_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM approval_executions WHERE approval_id = ?", (int(approval_id),)
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
                "meta_json) VALUES (?, ?, 'approval.execution_started', 'approval', ?, ?)",
                (now, executor_id, str(approval_id), meta),
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
                "meta_json) VALUES (?, ?, ?, 'approval', ?, ?)",
                (finished_at, executor_id, audit_action, str(approval_id), meta_json),
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
                "meta_json) VALUES (?, ?, 'approval.executed_reconciled', 'approval', ?, ?)",
                (finished_at, actor_id, str(approval_id), meta),
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
    ) -> int:
        """Create draft row, immutable version 1, and the creation audit atomically."""
        meta = json.dumps(
            {
                "guild_id": str(guild_id),
                "target_channel_id": str(target_channel_id),
                "content_type": content_type,
                "version": 1,
                "content_hash": content_hash,
            }
        )

        def _run(conn: sqlite3.Connection) -> int:
            draft = conn.execute(
                "INSERT INTO content_drafts (guild_id, target_channel_id, content_type, topic, "
                "audience, tone, purpose, created_by, created_at, updated_at, current_version, "
                "state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'draft')",
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
                "meta_json) VALUES (?, ?, 'content.draft_created', 'content_draft', ?, ?)",
                (created_at, created_by, str(draft_id), meta),
            )
            return draft_id

        return await self.db.run_in_transaction_with_result(_run)

    async def get_content_draft(self, draft_id: int, *, guild_id: str) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM content_drafts WHERE id = ? AND guild_id = ?",
            (int(draft_id), str(guild_id)),
        )

    async def get_content_draft_version(self, draft_id: int, version: int) -> sqlite3.Row | None:
        return await self.db.fetchone(
            "SELECT * FROM content_draft_versions WHERE draft_id = ? AND version = ?",
            (int(draft_id), int(version)),
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
                "meta_json) VALUES (?, ?, 'content.version_created', 'content_draft', ?, ?)",
                (created_at, created_by, str(draft_id), meta),
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
        meta = json.dumps({"approval_id": int(approval_id), "version": int(expected_version)})

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
                "meta_json) VALUES (?, ?, 'content.submitted', 'content_draft', ?, ?)",
                (updated_at, submitted_by, str(draft_id), meta),
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
