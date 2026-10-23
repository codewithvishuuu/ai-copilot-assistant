"""`/summarize` command: target choice, queue flow, persistence, and rendering."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import discord
from fakes import FakeInteraction, FakeMessage, FakeThread, fake_settings

from copilot.cogs.summary import SummaryCog
from copilot.core.analyzer import AnalysisResult
from copilot.core.collector import CollectedMessage, CollectedThread
from copilot.db.repository import Repository
from copilot.errors import (
    DatabaseError,
    JobCancelledError,
    NotAllowedError,
    ProviderUnavailableError,
    QueueFullError,
)
from copilot.llm.schemas import ThreadAnalysis


class FakeDiscordThread(discord.Thread):
    """A real ``discord.Thread`` subclass so the command's type check is exercised."""

    def __init__(
        self,
        *,
        thread_id: int = 1001,
        name: str = "Login flow is broken",
        guild_id: int = 500,
        parent_id: int | None = 42,
        opening: FakeMessage | None = None,
        messages: tuple | list = (),
        message_count: int | None = None,
    ) -> None:
        self._delegate = FakeThread(
            thread_id=thread_id,
            name=name,
            guild_id=guild_id,
            parent_id=parent_id,
            opening=opening,
            messages=messages,
            message_count=message_count,
        )
        self.id = self._delegate.id
        self.name = self._delegate.name
        self.guild = self._delegate.guild
        self.parent_id = self._delegate.parent_id
        self.message_count = self._delegate.message_count

    @property
    def jump_url(self) -> str:
        return self._delegate.jump_url

    @property
    def starter_message(self):
        return self._delegate.starter_message

    async def fetch_message(self, message_id):
        return await self._delegate.fetch_message(message_id)

    def history(self, *, limit=None, oldest_first=False):
        return self._delegate.history(limit=limit, oldest_first=oldest_first)


def _thread(
    *,
    thread_id: int = 1001,
    parent_id: int | None = 42,
    name: str = "Login flow is broken",
    guild_id: int = 500,
) -> FakeDiscordThread:
    opening = FakeMessage(id=thread_id, content="opening post", author="maya")
    reply = FakeMessage(id=thread_id + 1, content="still broken", author="kevin")
    return FakeDiscordThread(
        thread_id=thread_id,
        name=name,
        guild_id=guild_id,
        parent_id=parent_id,
        opening=opening,
        messages=[reply],
        message_count=2,
    )


def _collected(thread: FakeDiscordThread) -> CollectedThread:
    return CollectedThread(
        thread_id=str(thread.id),
        guild_id=str(thread.guild.id),
        parent_channel_id=str(thread.parent_id),
        thread_name=thread.name,
        jump_url=thread.jump_url,
        messages=[
            CollectedMessage(
                id=str(thread.id),
                author="maya",
                created_at="2026-10-18T12:00:00+00:00",
                content="opening post",
                is_opening=True,
            ),
            CollectedMessage(
                id=str(thread.id + 1),
                author="kevin",
                created_at="2026-10-18T12:05:00+00:00",
                content="still broken",
            ),
        ],
    )


def _result(collected: CollectedThread) -> AnalysisResult:
    return AnalysisResult(
        analysis=ThreadAnalysis.model_validate(
            {
                "overview": "Users cannot log in after the upgrade.",
                "main_problem": "Refresh token rejected.",
                "key_findings": [
                    {
                        "text": "Only SSO accounts affected.",
                        "source_message_ids": [collected.messages[0].id],
                    }
                ],
                "current_status": "in_progress",
                "status_confidence": 0.7,
                "suggested_next_step": "Capture the token exchange.",
            }
        ),
        collected=collected,
        model="fake-model",
        prompt_version="analysis-v1",
        elapsed_s=1.0,
    )


class RecordingAnalyzer:
    def __init__(self, *, result: AnalysisResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def analyze(self, thread, *, requested_by: str = "", max_messages: int | None = None):
        self.calls.append(
            {"thread": thread, "requested_by": requested_by, "max_messages": max_messages}
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class FakeQueue:
    """Immediate queue stand-in: submits, runs, and resolves the job deterministically."""

    def __init__(self, *, pending: int = 0, submit_error: Exception | None = None):
        self.pending = pending
        self.submit_error = submit_error
        self.submitted: list[dict] = []

    async def submit(self, *, user_id, guild_id, run):
        self.submitted.append({"user_id": user_id, "guild_id": guild_id, "run": run})
        if self.submit_error is not None:
            raise self.submit_error

        future = asyncio.get_running_loop().create_future()
        try:
            outcome = await run()
        except Exception as exc:  # noqa: BLE001 - mirrors the queue forwarding job errors
            future.set_exception(exc)
        else:
            future.set_result(outcome)
        return SimpleNamespace(id="job-1", future=future)

    def snapshot(self):
        return SimpleNamespace(pending=self.pending, active_job_id=None)


class FailingRepo:
    async def upsert_thread(self, **kwargs):
        raise DatabaseError()


def make_cog(tmp_path, *, queue, analyzer, repo=None, **settings_overrides) -> SummaryCog:
    settings = fake_settings(tmp_path, **settings_overrides)
    return SummaryCog(
        SimpleNamespace(settings=settings, queue=queue, analyzer=analyzer, repo=repo)
    )


async def _summarize(cog, interaction, thread=None, count=None):
    await SummaryCog.summarize.callback(cog, interaction, thread, count)


async def test_summarize_defaults_to_current_thread_and_persists_ephemeral_summary(
    tmp_path, tmp_db
):
    thread = _thread()
    collected = _collected(thread)
    analyzer = RecordingAnalyzer(result=_result(collected))
    queue = FakeQueue()
    repo = Repository(tmp_db)
    cog = make_cog(tmp_path, queue=queue, analyzer=analyzer, repo=repo)
    interaction = FakeInteraction(channel_id=thread.id, parent_id=thread.parent_id)
    interaction.channel = thread

    await _summarize(cog, interaction, count=25)

    assert interaction.response.deferred is True
    assert interaction.response.sent == []
    assert queue.submitted[0]["user_id"] == 7
    assert queue.submitted[0]["guild_id"] == 500
    assert analyzer.calls == [
        {"thread": thread, "requested_by": "7", "max_messages": 25}
    ]

    assert len(interaction.followup.sent) == 1
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert sent["content"] is None
    assert len(sent["embeds"]) == 1
    embed = sent["embeds"][0]
    assert embed.title == "Summary — Login flow is broken"
    assert embed.description == "Users cannot log in after the upgrade."
    assert "AI-generated — verify before acting" in embed.footer.text

    thread_row = await repo.get_thread_by_discord_id(str(thread.id))
    assert thread_row is not None
    assert thread_row["name"] == thread.name
    assert thread_row["last_summarized_at"]
    assert thread_row["retention_expires_at"]

    message_rows = await tmp_db.fetchall(
        "SELECT message_id, author_name, is_opening FROM messages "
        "WHERE thread_id = ? ORDER BY id",
        (thread_row["id"],),
    )
    assert [
        (row["message_id"], row["author_name"], row["is_opening"])
        for row in message_rows
    ] == [("1001", "maya", 1), ("1002", "kevin", 0)]

    summary = await repo.latest_summary(thread_row["id"])
    assert summary is not None
    assert summary["requested_by"] == "7"
    assert summary["model"] == "fake-model"
    assert summary["prompt_version"] == "analysis-v1"
    assert summary["status"] == "in_progress"
    assert summary["confidence"] == 0.7
    assert json.loads(summary["analysis_json"])["overview"] == (
        "Users cannot log in after the upgrade."
    )

    kb_rows = await tmp_db.fetchall(
        "SELECT guild_id, approved, title, body, url FROM kb_docs "
        "WHERE kind = 'thread' AND ref_id = ?",
        (thread_row["id"],),
    )
    assert len(kb_rows) == 1
    assert kb_rows[0]["guild_id"] == "500"
    assert kb_rows[0]["approved"] == 0
    assert kb_rows[0]["title"] == thread.name
    assert kb_rows[0]["url"] == thread.jump_url
    assert "Users cannot log in after the upgrade." in kb_rows[0]["body"]
    assert "Refresh token rejected." in kb_rows[0]["body"]


async def test_summarize_uses_explicit_thread_option_and_count(tmp_path, tmp_db):
    thread = _thread(thread_id=2001, name="Webhook outages")
    analyzer = RecordingAnalyzer(result=_result(_collected(thread)))
    queue = FakeQueue()
    cog = make_cog(
        tmp_path, queue=queue, analyzer=analyzer, repo=Repository(tmp_db)
    )
    interaction = FakeInteraction(channel_id=2002, parent_id=42)

    await _summarize(cog, interaction, thread, 5)

    assert analyzer.calls == [
        {"thread": thread, "requested_by": "7", "max_messages": 5}
    ]
    assert interaction.response.deferred is True
    assert len(interaction.followup.sent) == 1
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert interaction.followup.sent[0]["embeds"][0].title == "Summary — Webhook outages"


async def test_summarize_regular_channel_is_refused_before_defer(tmp_path):
    queue = FakeQueue()
    analyzer = RecordingAnalyzer()
    cog = make_cog(tmp_path, queue=queue, analyzer=analyzer)
    interaction = FakeInteraction()

    await _summarize(cog, interaction)

    assert interaction.response.deferred is None
    assert [message["content"] for message in interaction.response.sent] == [
        "Run this inside a forum post/thread, or pick one with the `thread` option. "
        "Regular channels have no thread history to summarize."
    ]
    assert interaction.followup.sent == []
    assert queue.submitted == []
    assert analyzer.calls == []


async def test_summarize_thread_option_outside_channel_allowlist_is_refused_before_defer(
    tmp_path,
):
    queue = FakeQueue()
    analyzer = RecordingAnalyzer()
    cog = make_cog(
        tmp_path, queue=queue, analyzer=analyzer, channel_allowlist=[42]
    )
    interaction = FakeInteraction(channel_id=2002, parent_id=42)
    thread = _thread(thread_id=3001, parent_id=999)

    await _summarize(cog, interaction, thread)

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    assert interaction.followup.sent == []
    assert queue.submitted == []
    assert analyzer.calls == []


async def test_summarize_thread_from_another_guild_is_refused_before_defer(tmp_path):
    queue = FakeQueue()
    analyzer = RecordingAnalyzer()
    cog = make_cog(tmp_path, queue=queue, analyzer=analyzer)
    interaction = FakeInteraction(guild_id=500, channel_id=2002, parent_id=42)
    thread = _thread(thread_id=3002, guild_id=999)

    await _summarize(cog, interaction, thread)

    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotAllowedError())
    assert interaction.followup.sent == []
    assert queue.submitted == []
    assert analyzer.calls == []


async def test_summarize_queue_error_is_reported_by_followup_after_defer(
    tmp_path, tmp_db
):
    error = QueueFullError()
    queue = FakeQueue(submit_error=error)
    analyzer = RecordingAnalyzer()
    cog = make_cog(
        tmp_path, queue=queue, analyzer=analyzer, repo=Repository(tmp_db)
    )
    interaction = FakeInteraction(channel_id=2002, parent_id=42)
    thread = _thread(thread_id=4001)

    await _summarize(cog, interaction, thread)

    assert interaction.response.deferred is True
    assert interaction.response.sent == []
    assert [message["content"] for message in interaction.followup.sent] == [
        error.user_message()
    ]
    assert analyzer.calls == []
    assert (await tmp_db.counts())["threads"] == 0


async def test_summarize_job_cancellation_is_reported_by_followup(tmp_path, tmp_db):
    analyzer = RecordingAnalyzer(error=JobCancelledError())
    queue = FakeQueue()
    cog = make_cog(
        tmp_path, queue=queue, analyzer=analyzer, repo=Repository(tmp_db)
    )
    interaction = FakeInteraction(channel_id=2002, parent_id=42)
    thread = _thread(thread_id=5001)

    await _summarize(cog, interaction, thread)

    assert interaction.response.deferred is True
    assert [message["content"] for message in interaction.followup.sent] == [
        "Summarization cancelled."
    ]
    assert analyzer.calls
    assert (await tmp_db.counts())["threads"] == 0


async def test_summarize_model_error_is_reported_by_followup(tmp_path, tmp_db):
    error = ProviderUnavailableError()
    analyzer = RecordingAnalyzer(error=error)
    queue = FakeQueue()
    cog = make_cog(
        tmp_path, queue=queue, analyzer=analyzer, repo=Repository(tmp_db)
    )
    interaction = FakeInteraction(channel_id=2002, parent_id=42)
    thread = _thread(thread_id=6001)

    await _summarize(cog, interaction, thread)

    assert interaction.response.deferred is True
    assert [message["content"] for message in interaction.followup.sent] == [
        error.user_message()
    ]
    assert (await tmp_db.counts())["threads"] == 0


async def test_summarize_shows_queued_notice_before_the_summary(tmp_path, tmp_db):
    thread = _thread(thread_id=7001)
    analyzer = RecordingAnalyzer(result=_result(_collected(thread)))
    queue = FakeQueue(pending=2)
    cog = make_cog(
        tmp_path, queue=queue, analyzer=analyzer, repo=Repository(tmp_db)
    )
    interaction = FakeInteraction(channel_id=2002, parent_id=42)

    await _summarize(cog, interaction, thread)

    assert len(interaction.followup.sent) == 2
    assert "Queued — 1 request(s) ahead" in interaction.followup.sent[0]["content"]
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert interaction.followup.sent[1]["embeds"][0].title == "Summary — Login flow is broken"


async def test_summarize_database_failure_still_renders_with_a_warning(tmp_path):
    thread = _thread(thread_id=8001)
    analyzer = RecordingAnalyzer(result=_result(_collected(thread)))
    queue = FakeQueue()
    cog = make_cog(tmp_path, queue=queue, analyzer=analyzer, repo=FailingRepo())
    interaction = FakeInteraction(channel_id=2002, parent_id=42)

    await _summarize(cog, interaction, thread)

    assert interaction.response.deferred is True
    assert len(interaction.followup.sent) == 1
    embed = interaction.followup.sent[0]["embeds"][0]
    notes = next(field.value for field in embed.fields if "Notes" in field.name)
    assert "this summary could not be saved to the local database" in notes
