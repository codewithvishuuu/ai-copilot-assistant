"""Retention: full per-thread purge (`/forget`), thread expiry sweep, and the
conversation-turn age sweep (Phase 2).

Thread deletion is complete: messages, summaries, corrections and solutions
cascade from ``threads``; knowledge-base documents pointing at the thread or at
its solutions are deleted explicitly (FTS rows follow via triggers).
Conversation turns are deleted by age only — never other tables.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .database import Database

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def expiry_from_now(days: int) -> str:
    return iso(utc_now() + timedelta(days=max(1, int(days))))


async def _purge_thread_rows(
    db: Database, thread_row_ids: list[int], *, preserve_approved: bool = False
) -> dict[str, int]:
    if not thread_row_ids:
        return {}
    placeholders = ",".join("?" for _ in thread_row_ids)
    params = tuple(thread_row_ids)
    approved_guard = " AND approved = 0" if preserve_approved else ""
    kb_filter = (
        f"(kind = 'thread' AND ref_id IN ({placeholders}){approved_guard}) OR "
        f"(kind = 'solution' AND ref_id IN "
        f"(SELECT id FROM solutions WHERE thread_id IN ({placeholders})){approved_guard})"
    )

    counts: dict[str, int] = {}
    for name, sql in (
        ("messages", f"SELECT COUNT(*) AS c FROM messages WHERE thread_id IN ({placeholders})"),
        ("summaries", f"SELECT COUNT(*) AS c FROM summaries WHERE thread_id IN ({placeholders})"),
        ("corrections", f"SELECT COUNT(*) AS c FROM corrections WHERE thread_id IN ({placeholders})"),
        ("solutions", f"SELECT COUNT(*) AS c FROM solutions WHERE thread_id IN ({placeholders})"),
    ):
        row = await db.fetchone(sql, params)
        counts[name] = int(row["c"]) if row is not None else 0

    kb_row = await db.fetchone(f"SELECT COUNT(*) AS c FROM kb_docs WHERE {kb_filter}", params + params)
    counts["kb_docs"] = int(kb_row["c"]) if kb_row is not None else 0

    statements = [
        (f"DELETE FROM kb_docs WHERE {kb_filter}", params + params),
        (f"DELETE FROM threads WHERE id IN ({placeholders})", params),
    ]
    await db.run_in_transaction(statements)
    log.info("Purged thread rows %s: %s (preserve_approved=%s)", thread_row_ids, counts, preserve_approved)
    return counts


async def purge_thread(
    db: Database, discord_thread_id: str, *, guild_id: str
) -> dict[str, int]:
    """Delete everything stored for one Discord thread. Returns per-table counts.

    The guild scope is mandatory: a thread owned by another guild is treated as
    absent, so a caller cannot purge across tenant boundaries even if its target
    authorization check is bypassed.
    """
    row = await db.fetchone(
        "SELECT id FROM threads WHERE discord_thread_id = ? AND guild_id = ?",
        (str(discord_thread_id), str(guild_id)),
    )
    if row is None:
        return {}
    return await _purge_thread_rows(db, [int(row["id"])])


async def sweep_expired(db: Database, *, days: int, now: datetime | None = None) -> dict[str, int]:
    """Expire old thread data while preserving explicitly approved knowledge.

    Unapproved thread fingerprints and pending candidates belong to the thread
    and are removed with it. An approved ``kb_docs`` row is a self-contained copy
    and survives the sweep; only the explicit moderator ``/forget`` purge deletes
    approved knowledge. Audit rows are never removed by retention.
    """
    current = iso(now or utc_now())
    rows = await db.fetchall(
        "SELECT id FROM threads WHERE retention_expires_at IS NOT NULL AND retention_expires_at < ?",
        (current,),
    )
    thread_ids = [int(row["id"]) for row in rows]
    if not thread_ids:
        return {}
    return await _purge_thread_rows(db, thread_ids, preserve_approved=True)


async def sweep_conversation_turns(db: Database, *, days: int, now: datetime | None = None) -> int:
    """Delete conversation turns older than ``days``; returns rows removed.

    One atomic DELETE, clamped to at least one day (mirrors ``expiry_from_now``).
    Runs from the same daily retention loop as ``sweep_expired``; the connection
    executes in a worker thread, so the Discord event loop never blocks. An
    in-flight chat request already holds its history snapshot, so deleting old
    rows can never corrupt a response being generated.
    """
    cutoff = iso((now or utc_now()) - timedelta(days=max(1, int(days))))
    return await db.execute("DELETE FROM conversation_turns WHERE created_at < ?", (cutoff,))


async def sweep_missions(db: Database, *, days: int, now: datetime | None = None) -> int:
    """Delete terminal missions (steps cascade) older than ``days``.

    Non-terminal missions are never touched by retention: they are either
    running or will be closed once by startup recovery.
    """
    cutoff = iso((now or utc_now()) - timedelta(days=max(1, int(days))))
    return await db.execute(
        "DELETE FROM missions WHERE status IN ('completed','failed','cancelled') "
        "AND finished_at IS NOT NULL AND finished_at < ?",
        (cutoff,),
    )


async def sweep_research_reports(db: Database, *, days: int, now: datetime | None = None) -> int:
    cutoff = iso((now or utc_now()) - timedelta(days=max(1, int(days))))
    return await db.execute("DELETE FROM research_reports WHERE created_at < ?", (cutoff,))


async def sweep_agent_eval_runs(db: Database, *, days: int, now: datetime | None = None) -> int:
    cutoff = iso((now or utc_now()) - timedelta(days=max(1, int(days))))
    return await db.execute("DELETE FROM agent_eval_runs WHERE created_at < ?", (cutoff,))


async def sweep_content_planning(db: Database, *, days: int, now: datetime | None = None) -> int:
    """Remove archived event plans and cancelled/done calendar rows past ``days``.

    Active drafts, planned entries and contextual reply drafts stay until a
    moderator dismisses them: they are visible to humans, not background noise.
    """
    cutoff = iso((now or utc_now()) - timedelta(days=max(1, int(days))))
    removed = await db.execute(
        "DELETE FROM event_plans WHERE state = 'archived' AND updated_at < ?", (cutoff,)
    )
    removed += await db.execute(
        "DELETE FROM content_calendar_entries WHERE state IN ('cancelled','done') "
        "AND updated_at < ?",
        (cutoff,),
    )
    removed += await db.execute(
        "DELETE FROM contextual_replies WHERE state = 'dismissed' AND created_at < ?",
        (cutoff,),
    )
    return removed
