"""Regression tests for the `/cancel` slash command (SummaryCog.cancel).

Live context: a first `/cancel` attempt showed "The application did not respond" and
the log held exactly one `Could not deliver interaction reply (HTTPException)` warning
whose details were discarded. These tests pin the command's contract: it always
acknowledges directly (no defer), replies exactly once per invocation, and its three
paths (no job / pending job / running job) behave deterministically.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fakes import FakeInteraction, fake_settings

from copilot.cogs.summary import SummaryCog
from copilot.core.queue import InferenceQueue
from copilot.errors import JobCancelledError


def make_cog(tmp_path, queue: InferenceQueue) -> SummaryCog:
    return SummaryCog(bot=SimpleNamespace(settings=fake_settings(tmp_path), queue=queue))


def new_queue() -> InferenceQueue:
    return InferenceQueue(
        capacity=2, per_user_cooldown_s=0, per_guild_hourly_limit=100, job_timeout_s=5.0
    )


async def _wait_until_active(queue: InferenceQueue, job_id: str, *, limit: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        if queue.snapshot().active_job_id == job_id:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never became active")


async def test_cancel_no_job_acks_without_defer_and_replies_once(tmp_path):
    interaction = FakeInteraction()  # default user id is 7
    cog = make_cog(tmp_path, new_queue())

    await SummaryCog.cancel.callback(cog, interaction)

    # The fast path IS the acknowledgement: no defer, exactly one reply.
    assert interaction.response.deferred is None
    assert [m["content"] for m in interaction.response.sent] == [
        "You have no pending or running requests."
    ]
    assert interaction.followup.sent == []


async def test_cancel_pending_job_marks_cancelled_and_replies(tmp_path):
    queue = new_queue()
    ran = False

    async def runner():
        nonlocal ran
        ran = True

    job = await queue.submit(user_id=7, guild_id=500, run=runner)
    interaction = FakeInteraction()
    cog = make_cog(tmp_path, queue)

    await SummaryCog.cancel.callback(cog, interaction)

    assert interaction.response.sent[0]["content"] == "Cancelling your request…"
    assert job.cancel_requested is True
    assert queue.snapshot().cancelled_total == 1
    with pytest.raises(JobCancelledError):
        await job.future
    assert ran is False  # the runner never executed


async def test_cancel_active_job_cancels_the_running_task(tmp_path):
    queue = new_queue()
    await queue.start()
    try:
        started = asyncio.Event()

        async def runner():
            started.set()
            await asyncio.Event().wait()  # runs until cancelled

        job = await queue.submit(user_id=7, guild_id=500, run=runner)
        await _wait_until_active(queue, job.id)
        assert started.is_set()

        interaction = FakeInteraction()
        cog = make_cog(tmp_path, queue)
        await SummaryCog.cancel.callback(cog, interaction)

        assert interaction.response.sent[0]["content"] == "Cancelling your request…"
        assert job.cancel_requested is True
        with pytest.raises(JobCancelledError):
            await job.future
    finally:
        await queue.stop()


async def test_second_cancel_reports_no_pending_after_cancellation_requested(tmp_path):
    queue = new_queue()

    async def runner():  # pragma: no cover - a cancelled job must never run
        raise AssertionError("cancelled jobs must not run")

    job = await queue.submit(user_id=7, guild_id=500, run=runner)
    cog = make_cog(tmp_path, queue)

    first = FakeInteraction()
    await SummaryCog.cancel.callback(cog, first)
    assert first.response.sent[0]["content"] == "Cancelling your request…"

    second = FakeInteraction()
    await SummaryCog.cancel.callback(cog, second)
    assert second.response.sent[0]["content"] == "You have no pending or running requests."

    with pytest.raises(JobCancelledError):
        await job.future
