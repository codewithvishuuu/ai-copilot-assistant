"""Async SQLite access built on the stdlib ``sqlite3`` module.

Design choices (see ARCHITECTURE.md):
- one connection, guarded by an ``asyncio.Lock``; all blocking calls run in
  threads via ``asyncio.to_thread`` (no aiosqlite dependency);
- autocommit mode; explicit transactions only through ``run_in_transaction``;
- WAL journaling, foreign keys ON, ``busy_timeout`` for robustness;
- versioned migrations parsed from ``schema.sql`` blocks (``-- @migration N``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from ..errors import DatabaseError

log = logging.getLogger(__name__)

T = TypeVar("T")

_MIGRATION_RE = re.compile(r"^-- @migration (\d+)\s*$", re.MULTILINE)

_KNOWN_TABLES = (
    "threads",
    "messages",
    "summaries",
    "corrections",
    "solutions",
    "kb_docs",
    "audit_log",
    "conversation_turns",
    "approval_requests",
    "approval_executions",
    "content_drafts",
    "content_draft_versions",
)


def parse_migrations(sql_text: str) -> list[tuple[int, str]]:
    """Split a schema file into ``(version, sql)`` migration blocks."""
    matches = list(_MIGRATION_RE.finditer(sql_text))
    migrations: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        version = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(sql_text)
        body = sql_text[start:end].strip()
        if body:
            migrations.append((version, body))
    versions = [v for v, _ in migrations]
    if versions != sorted(set(versions)):
        raise DatabaseError(
            "schema.sql has duplicate or unordered migration versions.", detail=str(versions)
        )
    return migrations


class Database:
    """Small async facade over one SQLite connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup

    async def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            self._conn = await asyncio.to_thread(self._connect_sync)
            await self._apply_migrations()
        except (sqlite3.Error, OSError) as exc:
            await self._discard_connection()
            raise DatabaseError(
                f"Could not open the local database at {self.path}.", detail=str(exc)
            ) from exc
        except BaseException:
            # A failed migration must not leave a half-open connection behind.
            await self._discard_connection()
            raise
        log.info("Database ready at %s", self.path)

    async def _discard_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await asyncio.to_thread(conn.close)
        except sqlite3.Error:  # pragma: no cover - close failure is not actionable
            log.warning("Could not close a failed database connection")

    def _connect_sync(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # autocommit; explicit BEGIN in run_in_transaction
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def _apply_migrations(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        try:
            text = schema_path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - packaging error
            raise DatabaseError("schema.sql is missing from the package.", detail=str(exc)) from exc
        migrations = parse_migrations(text)

        def _run() -> list[int]:
            conn = self._require_conn()
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            applied: list[int] = []
            for version, script in migrations:
                if version <= current:
                    continue
                # executescript() commits pending transactions first, then runs the
                # script as-is; our BEGIN/COMMIT keeps each migration atomic. On a
                # mid-block failure, roll back so the previous version and schema
                # remain intact rather than leaving a new column behind.
                try:
                    conn.executescript(
                        f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except sqlite3.Error:
                    if conn.in_transaction:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:  # pragma: no cover - rollback failure
                            log.exception("Migration rollback failed at version %s", version)
                    raise
                applied.append(version)
            return applied

        async with self._lock:
            applied = await asyncio.to_thread(_run)
        if applied:
            log.info("Applied database migrations: %s", applied)

    # -------------------------------------------------------------------- ops

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("The database is not connected.")
        return self._conn

    async def _guarded(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            try:
                return await asyncio.to_thread(fn)
            except sqlite3.Error as exc:
                raise DatabaseError(detail=str(exc)) from exc

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one statement (autocommit). Returns the affected row count."""

        def _run() -> int:
            cursor = self._require_conn().execute(sql, tuple(params))
            return int(cursor.rowcount) if cursor.rowcount is not None else 0

        return await self._guarded(_run)

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one INSERT (autocommit). Returns the new rowid."""

        def _run() -> int:
            cursor = self._require_conn().execute(sql, tuple(params))
            return int(cursor.lastrowid or 0)

        return await self._guarded(_run)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        def _run() -> sqlite3.Row | None:
            return self._require_conn().execute(sql, tuple(params)).fetchone()

        return await self._guarded(_run)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def _run() -> list[sqlite3.Row]:
            return list(self._require_conn().execute(sql, tuple(params)).fetchall())

        return await self._guarded(_run)

    async def run_in_transaction(self, statements: Iterable[tuple[str, Sequence[Any]]]) -> None:
        """Run several statements atomically (all-or-nothing)."""

        def _run() -> None:
            conn = self._require_conn()
            conn.execute("BEGIN")
            try:
                for sql, params in statements:
                    conn.execute(sql, tuple(params))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        await self._guarded(_run)

    async def run_in_transaction_with_result(
        self, operation: Callable[[sqlite3.Connection], T]
    ) -> T:
        """Run ``operation`` inside one transaction and return its result.

        The callback runs in the database worker thread and must only use the
        supplied connection. Any exception rolls the whole transaction back.
        """

        def _run() -> T:
            conn = self._require_conn()
            conn.execute("BEGIN")
            try:
                result = operation(conn)
                conn.execute("COMMIT")
                return result
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return await self._guarded(_run)

    async def ping(self) -> bool:
        try:
            await self.fetchone("SELECT 1")
            return True
        except DatabaseError:
            return False

    async def user_version(self) -> int:
        row = await self.fetchone("PRAGMA user_version")
        return int(row[0]) if row is not None else 0

    async def counts(self) -> dict[str, int]:
        """Row counts for the known tables (used by /status; truthful, no guessing)."""

        def _run() -> dict[str, int]:
            conn = self._require_conn()
            existing = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            out: dict[str, int] = {}
            for table in _KNOWN_TABLES:
                if table in existing:
                    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                    out[table] = int(row["c"])
            return out

        return await self._guarded(_run)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                await asyncio.to_thread(conn.close)
                log.info("Database connection closed")
