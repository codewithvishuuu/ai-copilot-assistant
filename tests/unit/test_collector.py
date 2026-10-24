"""Collector: caps, skip accounting, char budget, friendly errors."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fakes import FakeMessage, FakeThread, fake_settings

from copilot.core.collector import (
    CollectedAttachment,
    CollectedMessage,
    CollectedThread,
    apply_char_budget,
    collect_thread,
    is_skippable,
    to_collected,
)
from copilot.errors import EmptyThreadError, ThreadInaccessibleError


def _settings(tmp_path, **overrides):
    overrides.setdefault("fetch_timeout_s", 5.0)
    return fake_settings(tmp_path, **overrides)


# ------------------------------------------------------------------ collection


async def test_collect_orders_opening_first_then_chronological(tmp_path):
    opening = FakeMessage(id=1001, content="Login is broken since the update", author="maya")
    newest = FakeMessage(id=1003, content="still broken", author="kevin")
    older = FakeMessage(id=1002, content="tried clearing cookies", author="sam")
    thread = FakeThread(thread_id=1001, opening=opening, messages=[newest, older])

    collected = await collect_thread(thread, settings=_settings(tmp_path))

    assert [m.id for m in collected.messages] == ["1001", "1002", "1003"]
    assert collected.messages[0].is_opening is True
    assert collected.messages[0].author == "maya"
    assert collected.thread_id == "1001"
    assert collected.guild_id == "500"
    assert collected.parent_channel_id == "42"
    assert collected.thread_name == "Login flow is broken"
    assert collected.jump_url == "https://discord.com/channels/500/1001"
    assert collected.collected_at


async def test_collect_fetches_opening_via_fetch_message_when_starter_is_missing(tmp_path):
    class NoStarter(FakeThread):
        starter_message = None

    opening = FakeMessage(id=1001, content="opening post")
    thread = NoStarter(thread_id=1001, opening=opening, messages=[FakeMessage(id=1002, content="a reply")])

    collected = await collect_thread(thread, settings=_settings(tmp_path))
    assert [m.id for m in collected.messages] == ["1001", "1002"]
    assert collected.messages[0].is_opening is True


async def test_collect_falls_back_to_replies_when_opening_is_gone(tmp_path):
    class NoOpening(FakeThread):
        starter_message = None

        async def fetch_message(self, message_id):
            raise LookupError("message not found")

    thread = NoOpening(thread_id=1001, messages=[FakeMessage(id=1002, content="a reply")])
    collected = await collect_thread(thread, settings=_settings(tmp_path))

    assert [m.id for m in collected.messages] == ["1002"]
    assert any("opening post could not be read" in note for note in collected.skipped_notes)


async def test_collect_skips_system_and_empty_messages_with_notes(tmp_path):
    opening = FakeMessage(id=1001, content="opening")
    system = FakeMessage(id=1002, content="maya pinned a message", type=7)
    empty = FakeMessage(id=1003, content="")
    reply = FakeMessage(id=1004, content="a real reply")
    thread = FakeThread(thread_id=1001, opening=opening, messages=[reply, empty, system])

    collected = await collect_thread(thread, settings=_settings(tmp_path))

    assert [m.id for m in collected.messages] == ["1001", "1004"]
    assert "1 system/bot event message(s) skipped" in collected.skipped_notes
    assert "1 empty message(s) skipped" in collected.skipped_notes


async def test_collect_deduplicates_the_opening_from_history(tmp_path):
    opening = FakeMessage(id=1001, content="opening")
    duplicate = FakeMessage(id=1001, content="opening, seen again in history")
    reply = FakeMessage(id=1002, content="reply")
    thread = FakeThread(thread_id=1001, opening=opening, messages=[reply, duplicate])

    collected = await collect_thread(thread, settings=_settings(tmp_path))
    assert [m.id for m in collected.messages] == ["1001", "1002"]


async def test_collect_respects_message_limit_and_reports_thread_size(tmp_path):
    opening = FakeMessage(id=1001, content="opening")
    messages = [FakeMessage(id=2000 + n, content=f"message {n}") for n in range(10)]
    thread = FakeThread(thread_id=1001, opening=opening, messages=messages, message_count=42)

    collected = await collect_thread(thread, settings=_settings(tmp_path), max_messages=3)

    assert [m.id for m in collected.messages] == ["1001", "2002", "2001", "2000"]
    assert any("thread has about 42 messages" in note for note in collected.skipped_notes)


async def test_collect_trims_older_messages_to_fit_the_char_budget(tmp_path):
    opening = FakeMessage(id=1001, content="O" * 300)
    big_reply = FakeMessage(id=1002, content="B" * 900)
    small_reply = FakeMessage(id=1003, content="S" * 300)
    thread = FakeThread(thread_id=1001, opening=opening, messages=[small_reply, big_reply])

    collected = await collect_thread(thread, settings=_settings(tmp_path, max_chars=1000))

    assert [m.id for m in collected.messages] == ["1001", "1003"]
    assert "1 older message(s) trimmed to fit the character budget" in collected.skipped_notes
    assert collected.truncated is True


async def test_collect_truncates_an_oversized_opening(tmp_path):
    opening = FakeMessage(id=1001, content="A" * 3000)
    thread = FakeThread(thread_id=1001, opening=opening, messages=[])

    collected = await collect_thread(thread, settings=_settings(tmp_path, max_chars=1000))

    assert len(collected.messages) == 1
    assert len(collected.messages[0].content) <= 1000
    assert collected.messages[0].content.endswith("[truncated]")
    assert any("opening post was truncated" in note for note in collected.skipped_notes)
    assert collected.truncated is True


async def test_collect_empty_thread_raises(tmp_path):
    thread = FakeThread(thread_id=1001, opening=None, messages=[])
    with pytest.raises(EmptyThreadError):
        await collect_thread(thread, settings=_settings(tmp_path))


async def test_collect_history_failure_maps_to_thread_inaccessible(tmp_path):
    thread = FakeThread(
        thread_id=1001,
        messages=[FakeMessage(id=1002, content="x")],
        history_error=RuntimeError("boom"),
    )
    with pytest.raises(ThreadInaccessibleError) as excinfo:
        await collect_thread(thread, settings=_settings(tmp_path))
    assert "RuntimeError: boom" in (excinfo.value.detail or "")


async def test_collect_history_timeout_maps_to_thread_inaccessible(tmp_path):
    thread = FakeThread(
        thread_id=1001,
        messages=[FakeMessage(id=1002, content="x")],
        history_delay=0.3,
    )
    with pytest.raises(ThreadInaccessibleError) as excinfo:
        await collect_thread(thread, settings=_settings(tmp_path, fetch_timeout_s=0.05))
    assert excinfo.value.detail == "history fetch timeout"


# --------------------------------------------------------------------- helpers


def test_is_skippable_reasons():
    assert is_skippable(FakeMessage(id=1, content="hi")) is None
    assert is_skippable(FakeMessage(id=1, content="")) == "empty"
    assert is_skippable(FakeMessage(id=1, content="event", type=7)) == "system"
    assert is_skippable(FakeMessage(id=1, content="reply", type=19)) is None
    attachment_only = FakeMessage(id=1, content="", attachments=[SimpleNamespace()])
    assert is_skippable(attachment_only) is None  # attachment-only messages are usable


def test_to_collected_maps_fields_and_coerces_attachment_size():
    raw = FakeMessage(
        id=77,
        content=None,
        author="lina",
        created_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        attachments=[
            SimpleNamespace(
                filename="shot.png", content_type="image/png", size="1234", url="https://cdn.invalid/shot.png"
            ),
            SimpleNamespace(filename="bad.bin", content_type=None, size="not-a-number", url=None),
        ],
    )
    collected = to_collected(raw)

    assert collected.id == "77"
    assert collected.author == "lina"
    assert collected.created_at == "2026-10-18T12:00:00+00:00"
    assert collected.content == ""
    assert collected.attachments[0].size == 1234
    assert collected.attachments[0].message_id == "77"
    assert collected.attachments[1].size == 0
    assert collected.attachments[1].url == ""


def test_to_collected_handles_unknown_author_and_missing_datetime():
    raw = SimpleNamespace(id=9, content="x", author=None, created_at=None, attachments=None, type=0)
    collected = to_collected(raw)
    assert collected.author == "unknown"
    assert collected.created_at == ""


def test_apply_char_budget_drops_oldest_reply_first():
    messages = [
        CollectedMessage(id="1", author="a", created_at="", content="O" * 400, is_opening=True),
        CollectedMessage(id="2", author="a", created_at="", content="X" * 400),
        CollectedMessage(id="3", author="a", created_at="", content="Y" * 400),
    ]
    kept, dropped, opening_truncated = apply_char_budget(messages, 900)
    assert [m.id for m in kept] == ["1", "3"]
    assert dropped == 1
    assert opening_truncated is False


def test_apply_char_budget_leaves_small_threads_untouched():
    messages = [CollectedMessage(id="1", author="a", created_at="", content="abc", is_opening=True)]
    kept, dropped, opening_truncated = apply_char_budget(messages, 100)
    assert kept == messages
    assert dropped == 0
    assert opening_truncated is False


def test_looks_like_image_and_image_attachments():
    png = CollectedAttachment(filename="a.png", content_type=None, size=1, url="u", message_id="1")
    mime = CollectedAttachment(filename="noext", content_type="image/jpeg", size=1, url="u", message_id="1")
    other = CollectedAttachment(filename="doc.pdf", content_type="application/pdf", size=1, url="u", message_id="1")
    generic = CollectedAttachment(
        filename="upload.bin",
        content_type="application/octet-stream",
        size=1,
        url="u",
        message_id="1",
    )
    generic_pdf = CollectedAttachment(
        filename="upload.pdf",
        content_type="application/octet-stream",
        size=1,
        url="u",
        message_id="1",
    )
    assert png.looks_like_image() is True
    assert mime.looks_like_image() is True
    assert generic.looks_like_image() is True
    assert other.looks_like_image() is False
    assert generic_pdf.looks_like_image() is False

    thread = CollectedThread(
        thread_id="1",
        guild_id="1",
        parent_channel_id=None,
        thread_name="t",
        jump_url="u",
        messages=[
            CollectedMessage(id="1", author="a", created_at="", content="x", attachments=[png, other]),
            CollectedMessage(id="2", author="a", created_at="", content="y", attachments=[mime]),
        ],
    )
    assert [att.filename for att in thread.image_attachments()] == ["a.png", "noext"]
    assert thread.message_ids() == {"1", "2"}
