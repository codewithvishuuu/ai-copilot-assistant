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

    async def summaries_since(self, since_iso: str, *, limit: int = 1000) -> list[sqlite3.Row]:
        return await self.db.fetchall(
            "SELECT id, thread_id, created_at, status, error_signatures, analysis_json "
            "FROM summaries WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
            (since_iso, limit),
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
    ) -> int:
        return await self.db.insert(
            "INSERT INTO solutions (thread_id, body, source_url, state, proposed_by, proposed_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?)",
            (thread_id, body, source_url, actor_id, utc_now_iso()),
        )

    async def get_solution(self, solution_id: int) -> sqlite3.Row | None:
        return await self.db.fetchone("SELECT * FROM solutions WHERE id = ?", (solution_id,))

    async def set_solution_state(
        self,
        solution_id: int,
        *,
        state: str,
        decided_by: str,
        note: str | None = None,
    ) -> int:
        return await self.db.execute(
            "UPDATE solutions SET state = ?, decided_by = ?, decided_at = ?, "
            "note = COALESCE(?, note) WHERE id = ?",
            (state, decided_by, utc_now_iso(), note, solution_id),
        )

    # -------------------------------------------------------------- knowledge base

    async def insert_kb_doc(
        self,
        *,
        kind: str,
        ref_id: int | None,
        title: str,
        body: str,
        url: str | None,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO kb_docs (kind, ref_id, title, body, url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, ref_id, title, body, url, utc_now_iso()),
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

    async def search_kb(self, text: str, *, limit: int = 5) -> list[sqlite3.Row]:
        match = build_fts_query(text)
        if match is None:
            return []
        return await self.db.fetchall(
            "SELECT kb_docs.id, kb_docs.kind, kb_docs.title, kb_docs.body, kb_docs.url, "
            "kb_docs.created_at, snippet(kb_fts, 1, '[', ']', ' … ', 14) AS snippet, "
            "bm25(kb_fts) AS score "
            "FROM kb_fts JOIN kb_docs ON kb_docs.id = kb_fts.rowid "
            "WHERE kb_fts MATCH ? ORDER BY score LIMIT ?",
            (match, int(limit)),
        )

    async def related_docs(
        self,
        text: str,
        *,
        exclude_discord_thread_id: str | None = None,
        limit: int = 3,
    ) -> list[sqlite3.Row]:
        match = build_fts_query(text)
        if match is None:
            return []
        return await self.db.fetchall(
            "SELECT kb_docs.id, kb_docs.kind, kb_docs.title, kb_docs.body, kb_docs.url, "
            "kb_docs.created_at, snippet(kb_fts, 1, '[', ']', ' … ', 12) AS snippet, "
            "bm25(kb_fts) AS score, t.discord_thread_id AS source_thread "
            "FROM kb_fts JOIN kb_docs ON kb_docs.id = kb_fts.rowid "
            "LEFT JOIN threads t ON kb_docs.kind = 'thread' AND t.id = kb_docs.ref_id "
            "WHERE kb_fts MATCH ? AND (t.discord_thread_id IS NULL OR t.discord_thread_id <> ?) "
            "ORDER BY score LIMIT ?",
            (match, exclude_discord_thread_id or "", int(limit)),
        )

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
