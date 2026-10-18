"""Retention sweep + full per-thread purge (`/forget`).

Deletion is complete: messages, summaries, corrections and solutions cascade
from ``threads``; knowledge-base documents pointing at the thread or at its
solutions are deleted explicitly (FTS rows follow via triggers).
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


async def _purge_thread_rows(db: Database, thread_row_ids: list[int]) -> dict[str, int]:
    if not thread_row_ids:
        return {}
    placeholders = ",".join("?" for _ in thread_row_ids)
    params = tuple(thread_row_ids)

    counts: dict[str, int] = {}
    for name, sql in (
        ("messages", f"SELECT COUNT(*) AS c FROM messages WHERE thread_id IN ({placeholders})"),
        ("summaries", f"SELECT COUNT(*) AS c FROM summaries WHERE thread_id IN ({placeholders})"),
        ("corrections", f"SELECT COUNT(*) AS c FROM corrections WHERE thread_id IN ({placeholders})"),
        ("solutions", f"SELECT COUNT(*) AS c FROM solutions WHERE thread_id IN ({placeholders})"),
    ):
        row = await db.fetchone(sql, params)
        counts[name] = int(row["c"]) if row is not None else 0

    kb_row = await db.fetchone(
        f"SELECT COUNT(*) AS c FROM kb_docs WHERE "
        f"(kind = 'thread' AND ref_id IN ({placeholders})) OR "
        f"(kind = 'solution' AND ref_id IN "
        f"(SELECT id FROM solutions WHERE thread_id IN ({placeholders})))",
        params + params,
    )
    counts["kb_docs"] = int(kb_row["c"]) if kb_row is not None else 0

    statements = [
        (
            f"DELETE FROM kb_docs WHERE kind = 'thread' AND ref_id IN ({placeholders})",
            params,
        ),
        (
            "DELETE FROM kb_docs WHERE kind = 'solution' AND ref_id IN "
            f"(SELECT id FROM solutions WHERE thread_id IN ({placeholders}))",
            params,
        ),
        (f"DELETE FROM threads WHERE id IN ({placeholders})", params),
    ]
    await db.run_in_transaction(statements)
    log.info("Purged thread rows %s: %s", thread_row_ids, counts)
    return counts


async def purge_thread(db: Database, discord_thread_id: str) -> dict[str, int]:
    """Delete everything stored for one Discord thread. Returns per-table counts."""
    row = await db.fetchone(
        "SELECT id FROM threads WHERE discord_thread_id = ?", (str(discord_thread_id),)
    )
    if row is None:
        return {}
    return await _purge_thread_rows(db, [int(row["id"])])


async def sweep_expired(db: Database, *, days: int, now: datetime | None = None) -> dict[str, int]:
    """Delete threads whose retention window has passed (called by the daily task)."""
    current = iso(now or utc_now())
    rows = await db.fetchall(
        "SELECT id FROM threads WHERE retention_expires_at IS NOT NULL AND retention_expires_at < ?",
        (current,),
    )
    thread_ids = [int(row["id"]) for row in rows]
    if not thread_ids:
        return {}
    return await _purge_thread_rows(db, thread_ids)
