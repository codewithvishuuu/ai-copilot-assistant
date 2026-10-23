"""Persistent conversation memory (Phase 2): SQLite-backed with a bounded
in-memory fallback.

Contract:
- turns are scoped to ``(guild_id, channel_id, user_id)`` — the same isolation
  key the in-memory ``ConversationStore`` has always used. The model only ever
  sees the turns loaded for the caller's own scope; nothing else is reachable;
- the database is the source of truth: every read queries it first, so turns
  removed by retention or ``/clear-conversation`` can never be resurrected by
  a stale in-memory copy;
- the in-memory store remains as a same-process fallback for when the database
  is unavailable: chat keeps working, and the degradation is logged honestly;
- writes hit the database first (one transaction per exchange); the fallback
  is mirrored afterwards, so a failed write is never reported as persisted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db.repository import Repository
from ..errors import DatabaseError
from .conversation import ConversationStore, ScopeKey, Turn

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClearResult:
    """What a clear actually removed — reported to the user, never guessed."""

    deleted: int  # rows removed from the database
    memory_existed: bool  # an in-process fallback entry existed and was dropped
    persistent: bool  # whether a persistent store was available at all


@dataclass(frozen=True)
class MemoryStats:
    """What one scope actually holds — counts and timestamp bounds, never text."""

    turns: int
    oldest: str | None
    newest: str | None
    source: str  # "database" or "in-memory fallback (database unavailable)"


class ChatMemory:
    """Durable conversation turns (database first) with an in-memory fallback."""

    def __init__(
        self,
        *,
        repo: Repository | None,
        fallback: ConversationStore,
        max_turns: int,
        max_stored_turns: int,
    ) -> None:
        self._repo = repo
        self._fallback = fallback
        self._max_turns = max(1, int(max_turns))
        self._max_stored_turns = max(2, int(max_stored_turns))

    @property
    def persistent(self) -> bool:
        """True when a persistent store is wired up (used by the startup log)."""
        return self._repo is not None

    # ------------------------------------------------------------- read path

    async def history(self, key: ScopeKey) -> list[Turn]:
        """The bounded recent history for ``key`` (database first, oldest-first)."""
        params = self._params(key)
        if params is not None:
            try:
                rows = await self._repo.recent_conversation_turns(
                    guild_id=params[0],
                    channel_id=params[1],
                    user_id=params[2],
                    limit=self._max_turns * 2,
                )
            except DatabaseError as exc:
                log.warning(
                    "chat: persistent history unavailable (%s); using in-memory context only",
                    type(exc).__name__,
                )
            else:
                # The database answered: never merge in fallback entries, or a
                # clear/retention delete could appear to "come back".
                return [Turn(role=str(row["role"]), text=str(row["content"])) for row in rows]
        return self._fallback.history(key)

    # ------------------------------------------------------------ write path

    async def append(self, key: ScopeKey, user_text: str, assistant_text: str) -> bool:
        """Store one exchange. Returns True when it reached the database."""
        if not (user_text.strip() or assistant_text.strip()):
            return False
        persisted = False
        params = self._params(key)
        if params is not None:
            try:
                await self._repo.append_conversation_exchange(
                    guild_id=params[0],
                    channel_id=params[1],
                    user_id=params[2],
                    question=user_text,
                    answer=assistant_text,
                    keep_last=self._max_stored_turns,
                )
                persisted = True
            except DatabaseError as exc:
                log.warning(
                    "chat: could not persist the exchange (%s); keeping it in memory only",
                    type(exc).__name__,
                )
        self._fallback.append(key, user_text, assistant_text)
        return persisted

    async def stats(self, key: ScopeKey) -> MemoryStats:
        """Evidence view for one scope: counts and bounds only, never content."""
        params = self._params(key)
        if params is not None:
            try:
                row = await self._repo.conversation_turn_stats(
                    guild_id=params[0],
                    channel_id=params[1],
                    user_id=params[2],
                )
            except DatabaseError as exc:
                log.warning(
                    "chat: persistent memory stats unavailable (%s); reporting fallback only",
                    type(exc).__name__,
                )
            else:
                if row is not None:
                    return MemoryStats(
                        turns=int(row["turns"]),
                        oldest=None if row["oldest"] is None else str(row["oldest"]),
                        newest=None if row["newest"] is None else str(row["newest"]),
                        source="database",
                    )
        return MemoryStats(
            turns=len(self._fallback.history(key)),
            oldest=None,
            newest=None,
            source="in-memory fallback",
        )

    # ------------------------------------------------------------ clear path

    async def clear(self, key: ScopeKey) -> ClearResult:
        """Delete every stored turn for one scope (database first, then fallback).

        A database failure propagates without touching the fallback, so a
        partial clear can never be reported as success. The database delete is
        a single atomic statement.
        """
        params = self._params(key)
        deleted = 0
        if params is not None:
            deleted = await self._repo.delete_conversation_turns(
                guild_id=params[0],
                channel_id=params[1],
                user_id=params[2],
            )
        memory_existed = self._fallback.clear(key)
        return ClearResult(
            deleted=deleted,
            memory_existed=memory_existed,
            persistent=params is not None,
        )

    # -------------------------------------------------------------- internals

    def _params(self, key: ScopeKey) -> tuple[str, str, str] | None:
        """Map a scope key to database parameters; ``None`` = never persisted."""
        if self._repo is None:
            return None  # no persistent store wired up
        guild_id, channel_id, user_id = key
        if guild_id is None:
            return None  # no guild = no persistent scope (DMs are not supported)
        return (str(guild_id), str(channel_id), str(user_id))
