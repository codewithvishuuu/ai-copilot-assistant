"""Retention: full per-thread purge (`/forget`), thread expiry sweep, and the
conversation-turn age sweep (Phase 2).

Thread deletion covers stored thread rows: messages, summaries, corrections and
solutions cascade from ``threads``; knowledge-base documents pointing at the
thread or at its solutions are deleted explicitly (FTS rows follow via triggers).
The explicit `/forget` purge additionally removes channel-scoped runtime content
(conversation turns, moderation incidents/warnings, reply approvals, attachment
records) in the same transaction. Audit, content-draft, agent-run and scheduled
job records are operational history and are retained. The age sweeps are
separate and never touch other tables.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from .database import Database
from .repository import Repository

log = logging.getLogger(__name__)

_CHANNEL_SCOPED_TABLES = (
    "conversation_turns",
    "moderation_incidents",
    "reply_approvals",
    "attachments",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def expiry_from_now(days: int) -> str:
    return iso(utc_now() + timedelta(days=max(1, int(days))))


def _forget_audit_statement(
    *, actor_id: str, guild_id: str, target_id: str, counts: dict[str, int]
) -> tuple[str, tuple]:
    return (
        "INSERT INTO audit_log (created_at, actor_id, action, target_type, target_id, "
        "meta_json, guild_id) VALUES (?, ?, 'thread.forgotten', 'thread', ?, ?, ?)",
        (
            iso(utc_now()),
            actor_id,
            target_id,
            json.dumps({"guild_id": guild_id, "deleted": counts}),
            guild_id,
        ),
    )


async def _purge_thread_rows(
    db: Database,
    thread_row_ids: list[int],
    *,
    preserve_approved: bool = False,
    extra_counts: dict[str, int] | None = None,
    extra_statements: tuple[tuple[str, tuple], ...] = (),
    audit_actor_id: str | None = None,
    audit_guild_id: str | None = None,
    audit_target_id: str | None = None,
) -> dict[str, int]:
    """Delete thread rows plus any extra guild-scoped statements in one transaction.

    ``extra_counts``/``extra_statements`` let the explicit ``/forget`` purge add
    channel-scoped deletes (conversation turns, moderation records, reply
    approvals, attachments) atomically. Counts are reported only when non-zero,
    preserving the expiry sweep's existing shape.
    """
    if not thread_row_ids:
        # A thread with chat/moderator records but no ``threads`` row can still
        # be forgotten; the channel-scoped deletes stay atomic on their own.
        counts = dict(extra_counts or {})
        statements = list(extra_statements)
        if audit_actor_id is not None:
            statements.append(
                _forget_audit_statement(
                    actor_id=audit_actor_id,
                    guild_id=audit_guild_id or "",
                    target_id=audit_target_id or "",
                    counts=counts,
                )
            )
        if statements:
            await db.run_in_transaction(statements)
        return counts
    placeholders = ",".join("?" for _ in thread_row_ids)
    params = tuple(thread_row_ids)
    approved_guard = " AND approved = 0" if preserve_approved else ""
    kb_filter = (
        f"(kind = 'thread' AND ref_id IN ({placeholders}){approved_guard}) OR "
        f"(kind = 'solution' AND ref_id IN "
        f"(SELECT id FROM solutions WHERE thread_id IN ({placeholders})){approved_guard})"
    )

    counts: dict[str, int] = dict(extra_counts or {})
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
    statements.extend(extra_statements)
    if audit_actor_id is not None:
        statements.append(
            _forget_audit_statement(
                actor_id=audit_actor_id,
                guild_id=audit_guild_id or "",
                target_id=audit_target_id or "",
                counts=counts,
            )
        )
    await db.run_in_transaction(statements)
    log.info("Purged thread rows %s: %s (preserve_approved=%s)", thread_row_ids, counts, preserve_approved)
    return counts


async def purge_thread(
    db: Database,
    discord_thread_id: str,
    *,
    guild_id: str,
    audit_actor_id: str | None = None,
) -> dict[str, int]:
    """Delete stored content for one Discord thread. Returns per-table counts.

    Thread rows and channel-scoped runtime content are deleted in one
    guild-scoped transaction: conversation turns, moderation incidents (their
    warnings cascade), reply approvals and attachment records. Every query is
    scoped by guild, so another guild's rows are never touched even if the
    caller's target authorization check is bypassed. Audit, content-draft,
    agent-run and scheduled-job records are retained operational history.
    """
    row = await db.fetchone(
        "SELECT id FROM threads WHERE discord_thread_id = ? AND guild_id = ?",
        (str(discord_thread_id), str(guild_id)),
    )
    thread_ids = [] if row is None else [int(row["id"])]
    scope = (str(guild_id), str(discord_thread_id))

    extra_counts: dict[str, int] = {}
    extra_statements: list[tuple[str, tuple]] = []
    for table in _CHANNEL_SCOPED_TABLES:
        counted = await db.fetchone(
            f"SELECT COUNT(*) AS c FROM {table} WHERE guild_id = ? AND channel_id = ?",
            scope,
        )
        count = int(counted["c"]) if counted is not None else 0
        if count:
            extra_counts[table] = count
        extra_statements.append(
            (f"DELETE FROM {table} WHERE guild_id = ? AND channel_id = ?", scope)
        )
    if extra_counts.get("moderation_incidents"):
        warnings = await db.fetchone(
            "SELECT COUNT(*) AS c FROM moderation_warnings w "
            "JOIN moderation_incidents i ON i.id = w.incident_id "
            "WHERE i.guild_id = ? AND i.channel_id = ? AND w.guild_id = ?",
            (*scope, str(guild_id)),
        )
        warning_count = int(warnings["c"]) if warnings is not None else 0
        if warning_count:
            extra_counts["moderation_warnings"] = warning_count

    return await _purge_thread_rows(
        db,
        thread_ids,
        extra_counts=extra_counts,
        extra_statements=tuple(extra_statements),
        audit_actor_id=audit_actor_id,
        audit_guild_id=str(guild_id),
        audit_target_id=str(discord_thread_id),
    )


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


async def sweep_attachments(
    repo: Repository, *, days: int, now: datetime | None = None
) -> int:
    """Delete attachment metadata/extracted text older than ``days``.

    Reuses the repository delete so explicit channel/scope purges and the daily
    age sweep share one implementation. Runs independently of message ingestion.
    """
    cutoff = iso((now or utc_now()) - timedelta(days=max(1, int(days))))
    return await repo.purge_expired_attachments(before=cutoff)


async def sweep_reply_approvals(
    repo: Repository, *, now: datetime | None = None
) -> int:
    """Mark lapsed pending reply approvals expired; rows are never deleted.

    Reply approvals have a short TTL enforced on read; this daily sweep only
    catches rows whose expiry passed while the bot was offline. Expiry changes
    state and preserves the review history, matching approval-request expiry.
    """
    return await repo.expire_reply_approvals(now=iso(now or utc_now()))


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
