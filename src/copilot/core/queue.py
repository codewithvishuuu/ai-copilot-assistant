"""Single-worker inference queue with capacity, cooldowns, timeouts, cancellation.

All model calls in the bot go through this queue: exactly one job runs at a
time (matching a single CPU-loaded model), pending jobs are capped, and each
guild/user is rate limited. The queue is Discord-agnostic and fully unit tested.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..errors import (
    CooldownError,
    GuildLimitError,
    JobCancelledError,
    JobTimedOutError,
    QueueFullError,
)

log = logging.getLogger(__name__)

JobRunner = Callable[[], Awaitable[Any]]


@dataclass
class Job:
    """One unit of work (e.g. summarize a thread) owned by a user."""

    id: str
    user_id: int
    guild_id: int | None
    run: JobRunner
    timeout_s: float
    created_at: float
    future: asyncio.Future
    cancel_requested: bool = False
    started_at: float | None = None
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class QueueSnapshot:
    pending: int
    active_job_id: str | None
    active_user_id: int | None
    capacity: int
    processed_total: int
    cancelled_total: int
    failed_total: int
    cooldown_remaining_s: float


class InferenceQueue:
    """Sequential job queue. See ARCHITECTURE.md for the concurrency model."""

    def __init__(
        self,
        *,
        capacity: int,
        per_user_cooldown_s: int,
        per_guild_hourly_limit: int,
        job_timeout_s: float,
        hour_s: float = 3600.0,
    ) -> None:
        self.capacity = max(1, capacity)
        self.cooldown_s = max(0, per_user_cooldown_s)
        self.guild_hourly_limit = max(1, per_guild_hourly_limit)
        self.default_timeout_s = job_timeout_s
        self.hour_s = hour_s

        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=self.capacity)
        self._pending: dict[str, Job] = {}  # insertion-ordered mirror of pending jobs
        self._jobs: dict[str, Job] = {}
        self._active: Job | None = None
        self._worker: asyncio.Task | None = None
        self._last_submit: dict[int, float] = {}
        self._guild_events: dict[int, deque[float]] = {}
        self._counters = {"processed": 0, "cancelled": 0, "failed": 0}
        self._stopping = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopping = False
            self._worker = asyncio.create_task(self._worker_loop(), name="copilot-inference-worker")
            log.info("Inference queue started (capacity=%d)", self.capacity)

    async def stop(self) -> None:
        """Fail pending jobs fast and stop the worker (called on shutdown)."""
        self._stopping = True
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race safety
                break
            self._pending.pop(job.id, None)
            self._jobs.pop(job.id, None)
            if not job.future.done():
                job.future.set_exception(JobCancelledError("The bot is shutting down."))
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        self._worker = None
        log.info("Inference queue stopped")

    # -------------------------------------------------------------- submitting

    async def submit(
        self,
        *,
        user_id: int,
        guild_id: int | None,
        run: JobRunner,
        timeout_s: float | None = None,
    ) -> Job:
        """Enqueue a job. Raises CooldownError / GuildLimitError / QueueFullError."""
        if self._stopping:
            raise QueueFullError("The bot is shutting down — try again in a moment.")

        now = time.monotonic()

        last = self._last_submit.get(user_id)
        if last is not None and self.cooldown_s > 0:
            remaining = self.cooldown_s - (now - last)
            if remaining > 0:
                raise CooldownError(remaining)

        if guild_id is not None:
            events = self._guild_events.setdefault(guild_id, deque())
            cutoff = now - self.hour_s
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= self.guild_hourly_limit:
                retry = self.hour_s - (now - events[0])
                raise GuildLimitError(max(1.0, retry))

        if self._queue.full():
            raise QueueFullError()

        job = Job(
            id=uuid.uuid4().hex[:8],
            user_id=user_id,
            guild_id=guild_id,
            run=run,
            timeout_s=timeout_s if timeout_s is not None else self.default_timeout_s,
            created_at=now,
            future=asyncio.get_running_loop().create_future(),
        )
        self._jobs[job.id] = job
        self._pending[job.id] = job
        await self._queue.put(job)
        self._last_submit[user_id] = now
        if guild_id is not None:
            self._guild_events.setdefault(guild_id, deque()).append(now)
        log.info(
            "Job %s queued (user=%s guild=%s pending=%d)",
            job.id,
            user_id,
            guild_id,
            self._queue.qsize(),
        )
        return job

    # ------------------------------------------------------------- cancelling

    def cancel(self, job_id: str, user_id: int) -> bool:
        """Cancel a job; only its owner may cancel. Handles pending and active jobs."""
        job = self._jobs.get(job_id)
        if job is None or job.user_id != user_id or job.cancel_requested:
            return False
        job.cancel_requested = True
        if job.task is not None and not job.task.done():
            job.task.cancel()
        elif not job.future.done():
            job.future.set_exception(JobCancelledError())
        self._counters["cancelled"] += 1
        log.info("Job %s cancelled (user=%s)", job_id, user_id)
        return True

    def find_job_for_user(self, user_id: int) -> Job | None:
        """Active job first, then the oldest pending job for that user.

        Jobs whose cancellation was already requested are skipped, so /cancel
        always targets a job that can still be stopped.
        """
        if (
            self._active is not None
            and self._active.user_id == user_id
            and not self._active.cancel_requested
        ):
            return self._active
        for job in self._pending.values():
            if job.user_id == user_id and not job.cancel_requested:
                return job
        return None

    # -------------------------------------------------------------- reporting

    def snapshot(self, *, cooldown_for: int | None = None) -> QueueSnapshot:
        remaining = 0.0
        if cooldown_for is not None and self.cooldown_s > 0:
            last = self._last_submit.get(cooldown_for)
            if last is not None:
                remaining = max(0.0, self.cooldown_s - (time.monotonic() - last))
        return QueueSnapshot(
            pending=self._queue.qsize(),
            active_job_id=self._active.id if self._active else None,
            active_user_id=self._active.user_id if self._active else None,
            capacity=self.capacity,
            processed_total=self._counters["processed"],
            cancelled_total=self._counters["cancelled"],
            failed_total=self._counters["failed"],
            cooldown_remaining_s=remaining,
        )

    # ----------------------------------------------------------------- worker

    async def _worker_loop(self) -> None:
        log.debug("Inference worker started")
        while True:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                break
            self._pending.pop(job.id, None)

            if job.cancel_requested:
                # A cancelled job is never executed, even when its future was
                # already failed by cancel() while it was still pending.
                if not job.future.done():
                    job.future.set_exception(JobCancelledError())
                self._jobs.pop(job.id, None)
                continue

            self._active = job
            job.started_at = time.monotonic()
            try:
                task = asyncio.create_task(job.run(), name=f"copilot-job-{job.id}")
                job.task = task
                result = await asyncio.wait_for(task, timeout=job.timeout_s)
            except asyncio.CancelledError:
                if not job.future.done():
                    if job.cancel_requested:
                        job.future.set_exception(JobCancelledError())
                    else:
                        job.future.set_exception(JobCancelledError("The bot is shutting down."))
                if self._stopping:
                    raise
            except TimeoutError:
                if not job.future.done():
                    job.future.set_exception(JobTimedOutError())
                self._counters["failed"] += 1
                log.warning("Job %s timed out after %.0fs", job.id, job.timeout_s)
            except Exception as exc:  # noqa: BLE001 - forwarded to the awaiting command
                if not job.future.done():
                    job.future.set_exception(exc)
                self._counters["failed"] += 1
                log.info("Job %s failed: %s", job.id, type(exc).__name__)
            else:
                if not job.future.done():
                    job.future.set_result(result)
                self._counters["processed"] += 1
                elapsed = time.monotonic() - (job.started_at or time.monotonic())
                log.info("Job %s finished in %.1fs", job.id, elapsed)
            finally:
                self._active = None
                job.task = None
                self._jobs.pop(job.id, None)

            if self._stopping:
                # CPython 3.11's asyncio.wait_for() turns an external
                # cancellation into "return the finished task's result" when
                # the inner task completed in the same loop iteration
                # (Lib/asyncio/tasks.py:478). Without this check the worker
                # would loop back to queue.get() and stop() would wait on it
                # forever. Re-raising makes the shutdown deterministic.
                raise asyncio.CancelledError()
        log.debug("Inference worker exited")
