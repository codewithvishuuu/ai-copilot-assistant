"""Reusable fakes: no network, no real Discord, no real model.

These stand-ins are deliberately small and duck-typed so tests read like the
real flow instead of like a mock framework.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from copilot.llm.base import ModelCapabilities, ProviderHealth

FIXED_TIME = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------- provider


class FakeProvider:
    """Canned model responses, controllable capabilities and health."""

    def __init__(
        self,
        *,
        responses: list | None = None,
        reachable: bool = True,
        version: str = "9.9.9",
        model_exists: bool = True,
        vision: bool = True,
        thinking: bool = True,
        context_length: int | None = 131072,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self._reachable = reachable
        self._version = version
        self._model_exists = model_exists
        self._vision = vision
        self._thinking = thinking
        self._context_length = context_length

    async def chat(self, *, system, user, images=(), json_schema=None, timeout_s=None, max_tokens=None):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "images": list(images),
                "json_schema": json_schema,
            }
        )
        if not self.responses:
            return "{}"
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_messages(self, messages, *, images=(), json_schema=None, timeout_s=None, max_tokens=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "system": dict(messages[0]).get("content") if messages else None,
                "user": dict(messages[-1]).get("content") if messages else None,
                "images": list(images),
                "json_schema": json_schema,
                "timeout_s": timeout_s,
                "max_tokens": max_tokens,
            }
        )
        if not self.responses:
            return "{}"
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def health(self) -> ProviderHealth:
        if self._reachable:
            return ProviderHealth(reachable=True, version=self._version)
        return ProviderHealth(reachable=False, error="connection refused")

    async def capabilities(self, *, force_refresh: bool = False) -> ModelCapabilities:
        return ModelCapabilities(
            exists=self._model_exists,
            name="fake-model",
            supports_vision=self._vision,
            supports_thinking=self._thinking,
            context_length=self._context_length,
            error=None if self._model_exists else "model not installed",
        )


# -------------------------------------------------------------------- discord


class FakeAttachment:
    def __init__(
        self,
        *,
        filename: str = "image.png",
        content_type: str | None = "image/png",
        size: int | None = None,
        url: str = "https://example.invalid/img.png",
        data: bytes = b"",
        read_error: BaseException | None = None,
        read_delay: float = 0.0,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.data = data
        self.size = len(data) if size is None else size
        self.url = url
        self._read_error = read_error
        self._read_delay = read_delay

    async def read(self) -> bytes:
        if self._read_delay:
            await asyncio.sleep(self._read_delay)
        if self._read_error is not None:
            raise self._read_error
        return self.data


class FakeMessage:
    def __init__(
        self,
        *,
        id: int | str,
        content: str = "",
        author: str = "maya",
        created_at: datetime | None = None,
        attachments: tuple | list = (),
        type: int = 0,
    ) -> None:
        self.id = id
        self.content = content
        self.type = type
        self.author = SimpleNamespace(display_name=author, name=author, id=str(id))
        self.created_at = created_at or FIXED_TIME
        self.attachments = list(attachments)


class _FakeHistory:
    def __init__(self, messages, limit, error, delay):
        self._messages = list(messages[: (limit if limit else len(messages))])
        self._error = error
        self._delay = delay
        self._failed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None and not self._failed:
            self._failed = True
            raise self._error
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeThread:
    """Duck-typed `discord.Thread` stand-in used by collector/analyzer tests."""

    def __init__(
        self,
        *,
        thread_id: int | str = 1001,
        name: str = "Login flow is broken",
        guild_id: int | str = 500,
        parent_id: int | str | None = 42,
        opening: FakeMessage | None = None,
        messages: tuple | list = (),
        message_count: int | None = None,
        history_error: BaseException | None = None,
        history_delay: float = 0.0,
    ) -> None:
        self.id = thread_id
        self.name = name
        self.guild = SimpleNamespace(id=guild_id)
        self.parent_id = parent_id
        self.jump_url = f"https://discord.com/channels/{guild_id}/{thread_id}"
        self.message_count = message_count
        self._opening = opening
        self._messages = list(messages)
        self._history_error = history_error
        self._history_delay = history_delay

    @property
    def starter_message(self):
        return self._opening

    async def fetch_message(self, message_id):
        if self._opening is not None and str(self._opening.id) == str(message_id):
            return self._opening
        raise LookupError("message not found")

    def history(self, *, limit=None, oldest_first=False):
        return _FakeHistory(self._messages, limit, self._history_error, self._history_delay)


_MISSING = object()  # mirrors discord.py's MISSING sentinel: omitted vs explicitly passed


def _record_send(content, embed, embeds, ephemeral, view=_MISSING) -> dict:
    # Mirror discord.py's keyword rule (discord/http.py::handle_message_parameters):
    # passing BOTH `embed` and `embeds` raises TypeError — and an explicit None still
    # counts as "passed". Live bug CC-8480 slipped through because this fake used to
    # accept both silently.
    if embed is not _MISSING and embeds is not _MISSING:
        raise TypeError("Cannot mix embed and embeds keyword arguments.")
    return {
        "content": content,
        "embed": None if embed is _MISSING else embed,
        "embeds": None if embeds is _MISSING else embeds,
        "ephemeral": ephemeral,
        **({"view": view} if view is not _MISSING else {}),
    }


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deferred: bool | None = None
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(
        self,
        content=None,
        *,
        embed=_MISSING,
        embeds=_MISSING,
        ephemeral=False,
        allowed_mentions=None,
        view=_MISSING,
    ):
        self.sent.append(_record_send(content, embed, embeds, ephemeral, view))
        self._done = True

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = ephemeral
        self.thinking = thinking
        self._done = True

    async def send_modal(self, modal):
        self.sent.append({"modal": modal})
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(
        self,
        content=None,
        *,
        embed=_MISSING,
        embeds=_MISSING,
        ephemeral=False,
        allowed_mentions=None,
        view=_MISSING,
    ):
        self.sent.append(_record_send(content, embed, embeds, ephemeral, view))


def make_user(user_id: int = 7, *, manage_threads: bool = False, roles: tuple = ()):
    return SimpleNamespace(
        id=user_id,
        display_name="tester",
        guild_permissions=SimpleNamespace(manage_threads=manage_threads, manage_messages=False),
        roles=[SimpleNamespace(id=role_id) for role_id in roles],
    )


class FakeInteraction:
    def __init__(
        self,
        *,
        user=None,
        guild_id: int | None = 500,
        channel_id: int = 2002,
        parent_id: int | None = None,
    ) -> None:
        self.user = user if user is not None else make_user()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.response = FakeResponse()
        self.followup = FakeFollowup()


# ------------------------------------------------------------------ chat fakes


BOT_USER_ID = 999


def bot_user(bot_id: int = BOT_USER_ID):
    return SimpleNamespace(
        id=bot_id, bot=True, display_name="copilot", name="copilot", mention=f"<@{bot_id}>"
    )


class FakeTyping:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    async def __aenter__(self):
        self._channel.typing_events.append("start")
        return None

    async def __aexit__(self, *exc) -> bool:
        self._channel.typing_events.append("stop")
        return False


class FakeChannel:
    def __init__(self, *, channel_id: int = 2002, parent_id: int | None = None) -> None:
        self.id = channel_id
        self.parent_id = parent_id
        self.sent: list[dict] = []
        self.typing_events: list[str] = []
        self.send_error: BaseException | None = None  # raise on the NEXT (every) send

    def typing(self) -> FakeTyping:
        return FakeTyping(self)

    async def send(self, content=None, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"content": content, **kwargs})


def reply_reference(*, author_id: int, resolved: bool = True, message_id: int = 55):
    """A duck-typed ``discord.MessageReference`` (resolved from the client cache)."""
    if not resolved:
        return SimpleNamespace(resolved=None, message_id=message_id)
    return SimpleNamespace(
        resolved=SimpleNamespace(id=message_id, author=SimpleNamespace(id=author_id, bot=True)),
        message_id=message_id,
    )


class FakeChatMessage:
    """Duck-typed `discord.Message` for conversation-cog tests (guild messages only)."""

    def __init__(
        self,
        *,
        id: int,
        content: str = "",
        author_id: int = 7,
        author_bot: bool = False,
        guild_id: int | None = 500,
        channel_id: int = 2002,
        channel_parent_id: int | None = None,
        mentions: tuple | list = (),
        reference=None,
        attachments: tuple | list = (),
        type: int = 0,
        webhook_id: int | None = None,
    ) -> None:
        self.id = id
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=author_bot, display_name="tester")
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.channel = FakeChannel(channel_id=channel_id, parent_id=channel_parent_id)
        self.mentions = list(mentions)
        self.reference = reference
        self.attachments = list(attachments)
        self.type = type
        self.webhook_id = webhook_id
        self.replies: list[dict] = []
        self.reply_error: BaseException | None = None  # raise on the NEXT (every) reply

    async def reply(self, content=None, *, mention_author: bool = True, allowed_mentions=None, **kwargs):
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append(
            {
                "content": content,
                "mention_author": mention_author,
                "allowed_mentions": allowed_mentions,
            }
        )


# --------------------------------------------------------------------- settings


def fake_settings(tmp_path, **overrides):
    from copilot.config import Settings

    values = {
        "discord_token": "unit-test-token-0123456789",
        "guild_allowlist": [500],
        "max_messages": 50,
        "max_chars": 4000,
        "per_user_cooldown_s": 0,
        "conversation_cooldown_s": 0,
        "ollama_base_url": "http://127.0.0.1:1",
        "data_dir": tmp_path / "data",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
