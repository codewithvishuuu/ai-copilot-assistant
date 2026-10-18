"""Moderator solution workflow: propose -> approve/reject -> knowledge base.

Only approved solutions become searchable knowledge-base documents. Every
transition is audit-logged with actor and timestamp.
"""

from __future__ import annotations

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
    solution_id = await repo.insert_solution(
        thread_id=thread_row_id, body=body, source_url=source_url, actor_id=actor_id
    )
    await repo.audit(
        actor_id=actor_id,
        action="solution.proposed",
        target_type="solution",
        target_id=str(solution_id),
        meta={"thread_row_id": thread_row_id},
    )
    return solution_id


async def approve_solution(
    repo: Repository, *, solution_id: int, actor_id: str
) -> tuple[bool, str]:
    """Promote a proposed solution into the searchable knowledge base."""
    row = await repo.get_solution(solution_id)
    if row is None:
        return False, "not_found"
    if row["state"] != "proposed":
        return False, f"already_{row['state']}"

    thread_row = await repo.get_thread_row(int(row["thread_id"]))
    title = "Solution"
    if thread_row is not None and thread_row["name"]:
        title = f"Solution: {thread_row['name']}"

    await repo.set_solution_state(solution_id, state="approved", decided_by=actor_id)
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=solution_id,
        title=title,
        body=row["body"],
        url=row["source_url"] or thread_url(thread_row),
    )
    await repo.audit(
        actor_id=actor_id,
        action="solution.approved",
        target_type="solution",
        target_id=str(solution_id),
    )
    return True, "approved"


async def reject_solution(
    repo: Repository, *, solution_id: int, actor_id: str, note: str | None = None
) -> tuple[bool, str]:
    row = await repo.get_solution(solution_id)
    if row is None:
        return False, "not_found"
    if row["state"] != "proposed":
        return False, f"already_{row['state']}"
    await repo.set_solution_state(solution_id, state="rejected", decided_by=actor_id, note=note)
    await repo.audit(
        actor_id=actor_id,
        action="solution.rejected",
        target_type="solution",
        target_id=str(solution_id),
        meta={"note": note} if note else None,
    )
    return True, "rejected"
