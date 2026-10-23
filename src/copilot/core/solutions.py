"""Moderator solution workflow: propose -> approve/reject -> knowledge base.

Only approved solutions become searchable knowledge-base documents. Every
transition is audit-logged with actor and timestamp.
"""

from __future__ import annotations

import hashlib

from ..db.repository import Repository


def thread_url(row) -> str | None:
    """Build the Discord URL of a stored thread row (guild + thread id)."""
    guild_id = row["guild_id"] if row is not None else None
    thread_id = row["discord_thread_id"] if row is not None else None
    if guild_id and thread_id:
        return f"https://discord.com/channels/{guild_id}/{thread_id}"
    return None


async def propose_solution(
    repo: Repository,
    *,
    thread_row_id: int,
    body: str,
    source_url: str | None,
    actor_id: str,
) -> int:
    thread = await repo.get_thread_row(int(thread_row_id))
    guild_id = str(thread["guild_id"]) if thread is not None else None
    solution_id = await repo.insert_solution(
        thread_id=thread_row_id,
        body=body,
        source_url=source_url,
        actor_id=actor_id,
        # A manual proposal is a candidate too: store its exact-text hash so the
        # living-knowledge duplicate check can recognize it later.
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        source_kind="manual",
    )
    await repo.audit(
        actor_id=actor_id,
        action="solution.proposed",
        target_type="solution",
        target_id=str(solution_id),
        guild_id=guild_id,
        meta={"guild_id": guild_id or "", "thread_row_id": thread_row_id},
    )
    return solution_id


async def approve_solution(
    repo: Repository, *, solution_id: int, guild_id: str, actor_id: str
) -> tuple[bool, str]:
    """Promote a guild-owned proposed solution into the searchable knowledge base.

    The state change, approved KB row, and audit event are one transaction. A
    summary-backed candidate is refused as stale if its source summary is no
    longer the latest for that thread.
    """
    outcome = await repo.approve_solution_with_kb(
        solution_id, guild_id=guild_id, actor_id=actor_id
    )
    return (outcome == "approved", outcome)


async def reject_solution(
    repo: Repository,
    *,
    solution_id: int,
    guild_id: str,
    actor_id: str,
    note: str | None = None,
) -> tuple[bool, str]:
    row = await repo.get_solution_for_guild(solution_id, guild_id=guild_id)
    if row is None:
        return False, "not_found"
    if row["state"] != "proposed":
        return False, f"already_{row['state']}"
    applied = await repo.set_solution_state(
        solution_id,
        state="rejected",
        decided_by=actor_id,
        note=note,
        expected_state="proposed",
    )
    if applied != 1:
        current = await repo.get_solution_for_guild(solution_id, guild_id=guild_id)
        if current is None:
            return False, "not_found"
        return False, f"already_{current['state']}"
    await repo.audit(
        actor_id=actor_id,
        action="solution.rejected",
        target_type="solution",
        target_id=str(solution_id),
        guild_id=str(guild_id),
        # The note itself stays on the solution row; audit metadata records only
        # whether/how much free text was supplied, never a copy of it.
        meta={
            "guild_id": str(guild_id),
            "note_present": bool(note),
            "note_chars": len(note or ""),
        },
    )
    return True, "rejected"
