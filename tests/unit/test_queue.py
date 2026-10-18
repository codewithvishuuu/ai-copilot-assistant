"""Inference queue semantics: serialization, capacity, cooldowns, cancellation, timeouts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from copilot.core.queue import InferenceQueue
from copilot.errors import (
    CooldownError,
    GuildLimitError,
    JobCancelledError,
    JobTimedOutError,
    QueueFullError,
)


@asynccontextmanager
async def running_queue(**overrides):
    queue = InferenceQueue(
        capacity=overrides.pop("capacity", 5),
        per_user_cooldown_s=overrides.pop("per_user_cooldown_s", 0),
        per_guild_hourly_limit=overrides.pop("per_guild_hourly_limit", 100),
        job_timeout_s=overrides.pop("job_timeout_s", 5.0),
    )
    await queue.start()
    try:
        yield queue
    finally:
        await queue.stop()


async def _wait_until_active(queue: InferenceQueue, job_id: str, *, limit: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        if queue.snapshot().active_job_id == job_id:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never became active")


async def test_jobs_run_sequentially_in_fifo_order():
    async with running_queue() as queue:
        concurrent = 0
        max_concurrent = 0
        order: list[int] = []

        async def work(number: int) -> int:
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.03)
            order.append(number)
            concurrent -= 1
            return number

        jobs = [
            await queue.submit(user_id=index + 1, guild_id=1, run=(lambda n=index: work(n)))
            for index in range(3)
        ]
        results = [await job.future for job in jobs]
        assert results == [0, 1, 2]
        assert order == [0, 1, 2]
        assert max_concurrent == 1
        snapshot = queue.snapshot()
        assert snapshot.processed_total == 3
        assert snapshot.pending == 0


async def test_capacity_limit_rejects_when_full():
    async with running_queue(capacity=1) as queue:
        release = asyncio.Event()

        async def blocking():
            await release.wait()
            return "ok"

        first = await queue.submit(user_id=1, guild_id=1, run=blocking)
        await _wait_until_active(queue, first.id)
        second = await queue.submit(user_id=2, guild_id=1, run=blocking)  # fills the pending slot
        with pytest.raises(QueueFullError):
            await queue.submit(user_id=3, guild_id=1, run=blocking)
        release.set()
        assert await first.future == "ok"
        assert await second.future == "ok"


async def test_per_user_cooldown_blocks_second_submission():
    async with running_queue(per_user_cooldown_s=60) as queue:
        first = await queue.submit(user_id=1, guild_id=1, run=lambda: _quick())
        assert await first.future == "quick"
        with pytest.raises(CooldownError) as excinfo:
            await queue.submit(user_id=1, guild_id=1, run=lambda: _quick())
        assert excinfo.value.retry_after_s > 0
        # a different user is unaffected
        second = await queue.submit(user_id=2, guild_id=1, run=lambda: _quick())
        assert await second.future == "quick"


async def test_guild_hourly_limit():
    async with running_queue(per_guild_hourly_limit=1) as queue:
        first = await queue.submit(user_id=1, guild_id=100, run=lambda: _quick())
        assert await first.future == "quick"
        with pytest.raises(GuildLimitError):
            await queue.submit(user_id=2, guild_id=100, run=lambda: _quick())
        # another guild is fine
        other = await queue.submit(user_id=3, guild_id=200, run=lambda: _quick())
        assert await other.future == "quick"


async def test_cancel_pending_job():
    async with running_queue(capacity=1) as queue:
        release = asyncio.Event()

        async def blocking():
            await release.wait()
            return "done"

        first = await queue.submit(user_id=1, guild_id=1, run=blocking)
        await _wait_until_active(queue, first.id)
        second = await queue.submit(user_id=2, guild_id=1, run=lambda: _quick())
        assert queue.cancel(second.id, user_id=2) is True
        with pytest.raises(JobCancelledError):
            await second.future
        release.set()
        assert await first.future == "done"
        assert queue.snapshot().cancelled_total == 1


async def test_cancel_active_job():
    async with running_queue() as queue:
        release = asyncio.Event()

        async def blocking():
            await release.wait()
            return "never"

        job = await queue.submit(user_id=1, guild_id=1, run=blocking)
        await _wait_until_active(queue, job.id)
        assert queue.cancel(job.id, user_id=1) is True
        with pytest.raises(JobCancelledError):
            await job.future
        release.set()
        await asyncio.sleep(0.05)  # let the worker settle back to idle
        assert queue.snapshot().active_job_id is None


async def test_cancel_requires_ownership_and_only_once():
    async with running_queue(capacity=1) as queue:
        release = asyncio.Event()

        async def blocking():
            await release.wait()

        first = await queue.submit(user_id=1, guild_id=1, run=blocking)
        await _wait_until_active(queue, first.id)
        second = await queue.submit(user_id=2, guild_id=1, run=lambda: _quick())
        assert queue.cancel(second.id, user_id=9) is False  # not the owner
        assert queue.cancel(second.id, user_id=2) is True
        assert queue.cancel(second.id, user_id=2) is False  # already requested
        with pytest.raises(JobCancelledError):
            await second.future
        release.set()
        await first.future


async def test_job_timeout_marks_failure_and_frees_the_worker():
    async with running_queue(job_timeout_s=0.05) as queue:
        async def slow():
            await asyncio.sleep(1.0)
            return "late"

        job = await queue.submit(user_id=1, guild_id=1, run=slow)
        with pytest.raises(JobTimedOutError):
            await job.future
        assert queue.snapshot().failed_total >= 1

        # the worker is still usable afterwards
        follow_up = await queue.submit(user_id=2, guild_id=1, run=lambda: _quick())
        assert await follow_up.future == "quick"


async def test_exceptions_propagate_to_awaiter_only():
    async with running_queue() as queue:
        async def boom():
            raise ValueError("kaboom")

        job = await queue.submit(user_id=1, guild_id=1, run=boom)
        with pytest.raises(ValueError):
            await job.future
        assert queue.snapshot().failed_total >= 1
        # worker survives
        ok = await queue.submit(user_id=2, guild_id=1, run=lambda: _quick())
        assert await ok.future == "quick"


async def test_stop_fails_pending_and_active_jobs():
    queue = InferenceQueue(
        capacity=5, per_user_cooldown_s=0, per_guild_hourly_limit=100, job_timeout_s=5.0
    )
    await queue.start()
    release = asyncio.Event()

    async def blocking():
        await release.wait()

    active = await queue.submit(user_id=1, guild_id=1, run=blocking)
    await _wait_until_active(queue, active.id)
    pending = await queue.submit(user_id=2, guild_id=1, run=lambda: _quick())

    await queue.stop()
    with pytest.raises(JobCancelledError):
        await pending.future
    with pytest.raises(JobCancelledError):
        await active.future

    # submitting after stop is refused
    with pytest.raises(QueueFullError):
        await queue.submit(user_id=3, guild_id=1, run=lambda: _quick())


async def test_stop_survives_wait_for_swallowing_cancellation():
    """Regression (CPython 3.11): asyncio.wait_for() returns the inner task's
    result instead of propagating an external cancellation when that task
    finished in the same loop iteration (Lib/asyncio/tasks.py:478). stop()
    must still complete instead of waiting on a worker that went back to
    ``queue.get()``."""
    queue = InferenceQueue(
        capacity=1, per_user_cooldown_s=0, per_guild_hourly_limit=10, job_timeout_s=5.0
    )
    await queue.start()

    async def quick():
        return "quick"

    await queue.submit(user_id=1, guild_id=1, run=quick)
    # Iteration 1: the worker dequeues the job and suspends inside wait_for.
    await asyncio.sleep(0)
    # Iteration 2: the job task completes and wait_for's release callback is
    # queued, but the worker has not observed it yet. stop() now hits the
    # exact window where 3.11's wait_for() would swallow the cancellation.
    await asyncio.sleep(0)

    await asyncio.wait_for(queue.stop(), timeout=3)  # must not hang
    assert queue.snapshot().active_job_id is None


async def test_find_job_for_user_prefers_active_then_oldest_pending():
    async with running_queue(capacity=3) as queue:
        release = asyncio.Event()

        async def blocking():
            await release.wait()
            return "done"

        active = await queue.submit(user_id=5, guild_id=1, run=blocking)
        await _wait_until_active(queue, active.id)
        assert queue.find_job_for_user(5) is active

        first_pending = await queue.submit(user_id=6, guild_id=1, run=lambda: _quick())
        second_pending = await queue.submit(user_id=6, guild_id=1, run=lambda: _quick())
        assert queue.find_job_for_user(6) is first_pending

        assert queue.cancel(first_pending.id, user_id=6) is True
        with pytest.raises(JobCancelledError):
            await first_pending.future
        # already-cancelled jobs are never returned again
        assert queue.find_job_for_user(6) is second_pending

        assert queue.cancel(second_pending.id, user_id=6) is True
        with pytest.raises(JobCancelledError):
            await second_pending.future
        assert queue.find_job_for_user(6) is None

        release.set()
        assert await active.future == "done"


async def test_snapshot_reports_cooldown_remaining():
    async with running_queue(per_user_cooldown_s=60) as queue:
        job = await queue.submit(user_id=42, guild_id=1, run=lambda: _quick())
        await job.future
        snapshot = queue.snapshot(cooldown_for=42)
        assert 0 < snapshot.cooldown_remaining_s <= 60
        assert queue.snapshot(cooldown_for=43).cooldown_remaining_s == 0


async def _quick():
    return "quick"
