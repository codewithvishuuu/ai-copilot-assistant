"""Ollama provider: request shape, schema fallback, timeouts, redaction, caching."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from types import SimpleNamespace

import httpx
import pytest
from fakes import fake_settings
from ollama import ResponseError

from copilot.errors import (
    ModelNotInstalledError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from copilot.llm.ollama_provider import OllamaProvider, strip_thinking


class FakeChatClient:
    """Stands in for ``ollama.AsyncClient`` (no sockets)."""

    def __init__(self) -> None:
        self.chat_calls: list[dict] = []
        self.show_calls: list[str] = []
        self.chat_response: object = {"message": {"content": "{}"}}
        self.chat_errors: deque[BaseException] = deque()
        self.chat_delay = 0.0
        self.show_response: object = None
        self.show_error: BaseException | None = None

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        if self.chat_delay:
            await asyncio.sleep(self.chat_delay)
        if self.chat_errors:
            raise self.chat_errors.popleft()
        return self.chat_response

    async def show(self, model):
        self.show_calls.append(model)
        if self.show_error is not None:
            raise self.show_error
        return self.show_response


class FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, *, version: str = "0.40.2", error: BaseException | None = None) -> None:
        self.version = version
        self.error = error
        self.gets: list[str] = []

    async def get(self, path: str) -> FakeHttpResponse:
        self.gets.append(path)
        if self.error is not None:
            raise self.error
        return FakeHttpResponse({"version": self.version})

    async def aclose(self) -> None:
        return None


def _provider(tmp_path, *, chat=None, http=None, **overrides):
    settings = fake_settings(tmp_path, **overrides)
    return OllamaProvider(
        settings,
        client=chat if chat is not None else FakeChatClient(),
        http_client=http if http is not None else FakeHttpClient(),
    )


# ------------------------------------------------------------------------ chat


async def test_chat_sends_expected_request_and_strips_thinking(tmp_path):
    chat = FakeChatClient()
    chat.chat_response = {"message": {"content": '<|think|>{"overview": "ok"}'}}
    provider = _provider(tmp_path, chat=chat)

    text = await provider.chat(system="sys", user="usr", json_schema={"type": "object"})
    assert text == '{"overview": "ok"}'

    call = chat.chat_calls[0]
    assert call["model"] == "gemma4:e2b"
    assert call["stream"] is False
    assert call["think"] is False  # THINKING_ENABLED defaults to off for reliability
    assert call["format"] == {"type": "object"}
    assert call["keep_alive"] == "5m"
    assert call["options"]["num_ctx"] == 4096
    assert call["options"]["num_predict"] == 1024
    assert call["options"]["temperature"] == 0.3
    assert call["messages"][0] == {"role": "system", "content": "sys"}
    assert call["messages"][1]["content"] == "usr"
    assert "images" not in call["messages"][1]


async def test_chat_encodes_images_for_vision_models(tmp_path):
    chat = FakeChatClient()
    provider = _provider(tmp_path, chat=chat)
    await provider.chat(system="s", user="u", images=[b"\x89PNG", b"x"])
    images = chat.chat_calls[0]["messages"][1]["images"]
    assert images == [
        base64.b64encode(b"\x89PNG").decode("ascii"),
        base64.b64encode(b"x").decode("ascii"),
    ]


async def test_thinking_opt_in_is_forwarded(tmp_path):
    chat = FakeChatClient()
    provider = _provider(tmp_path, chat=chat, thinking_enabled=True)
    await provider.chat(system="s", user="u")
    assert chat.chat_calls[0]["think"] is True


async def test_missing_message_returns_empty_string(tmp_path):
    chat = FakeChatClient()
    chat.chat_response = {"message": None}
    provider = _provider(tmp_path, chat=chat)
    assert await provider.chat(system="s", user="u") == ""


async def test_chat_timeout_maps_to_provider_timeout(tmp_path):
    chat = FakeChatClient()
    chat.chat_delay = 0.3
    provider = _provider(tmp_path, chat=chat)
    with pytest.raises(ProviderTimeoutError):
        await provider.chat(system="s", user="u", timeout_s=0.05)


async def test_schema_rejection_falls_back_to_json_mode_once(tmp_path):
    chat = FakeChatClient()
    chat.chat_errors.append(ResponseError("invalid format: schema unsupported", 400))
    chat.chat_response = {"message": {"content": '{"overview": "ok"}'}}
    provider = _provider(tmp_path, chat=chat)

    text = await provider.chat(system="s", user="u", json_schema={"type": "object"})
    assert text == '{"overview": "ok"}'
    assert len(chat.chat_calls) == 2
    assert chat.chat_calls[0]["format"] == {"type": "object"}
    assert chat.chat_calls[1]["format"] == "json"

    # once learned, every later call goes straight to JSON mode
    await provider.chat(system="s", user="u", json_schema={"type": "object"})
    assert len(chat.chat_calls) == 3
    assert chat.chat_calls[2]["format"] == "json"


async def test_failed_schema_fallback_raises_without_a_third_attempt(tmp_path):
    chat = FakeChatClient()
    chat.chat_errors.append(ResponseError("invalid format: schema rejected", 400))
    chat.chat_errors.append(ResponseError("still failing", 500))
    provider = _provider(tmp_path, chat=chat)
    with pytest.raises(ProviderError) as excinfo:
        await provider.chat(system="s", user="u", json_schema={"type": "object"})
    assert "still failing" in (excinfo.value.detail or "")
    assert len(chat.chat_calls) == 2  # exactly one fallback attempt
    assert chat.chat_calls[1]["format"] == "json"


async def test_errors_after_schema_support_is_known_are_not_retried(tmp_path):
    chat = FakeChatClient()
    chat.chat_errors.append(ResponseError("schema rejected", 400))
    provider = _provider(tmp_path, chat=chat)

    # First call learns that schemas are unsupported: one fallback, then success.
    await provider.chat(system="s", user="u", json_schema={"type": "object"})
    assert len(chat.chat_calls) == 2

    chat.chat_errors.append(ResponseError("backend exploded", 500))
    with pytest.raises(ProviderError) as excinfo:
        await provider.chat(system="s", user="u", json_schema={"type": "object"})
    assert "backend exploded" in (excinfo.value.detail or "")
    assert len(chat.chat_calls) == 3  # no retry: support status already known


async def test_404_maps_to_model_not_installed(tmp_path):
    chat = FakeChatClient()
    chat.chat_errors.append(ResponseError("model 'gemma4:e2b' not found", 404))
    provider = _provider(tmp_path, chat=chat)
    with pytest.raises(ModelNotInstalledError):
        await provider.chat(system="s", user="u")


async def test_connection_errors_map_to_unavailable(tmp_path):
    chat = FakeChatClient()
    chat.chat_errors.append(httpx.ConnectError("connection refused"))
    provider = _provider(tmp_path, chat=chat)
    with pytest.raises(ProviderUnavailableError):
        await provider.chat(system="s", user="u")


async def test_error_details_are_redacted(tmp_path):
    chat = FakeChatClient()
    chat.chat_errors.append(ResponseError("bad credentials token=super-secret-value", 500))
    provider = _provider(tmp_path, chat=chat)
    with pytest.raises(ProviderError) as excinfo:
        await provider.chat(system="s", user="u")
    assert "super-secret-value" not in (excinfo.value.detail or "")
    assert "[REDACTED]" in (excinfo.value.detail or "")


# ---------------------------------------------------------------------- health


async def test_health_ok_reports_version(tmp_path):
    provider = _provider(tmp_path, http=FakeHttpClient(version="0.40.2"))
    health = await provider.health()
    assert health.reachable is True
    assert health.version == "0.40.2"


async def test_health_unreachable_reports_redacted_error(tmp_path):
    provider = _provider(tmp_path, http=FakeHttpClient(error=httpx.ConnectError("connection refused")))
    health = await provider.health()
    assert health.reachable is False
    assert "connection refused" in (health.error or "")


# ---------------------------------------------------------------- capabilities


async def test_capabilities_parse_show_payload(tmp_path):
    chat = FakeChatClient()
    chat.show_response = {
        "capabilities": ["completion", "vision", "thinking"],
        "model_info": {"gemma4.attention.head_count": 8, "gemma4.context_length": 131072},
    }
    provider = _provider(tmp_path, chat=chat)
    caps = await provider.capabilities()
    assert caps.exists is True
    assert caps.name == "gemma4:e2b"
    assert caps.supports_vision is True
    assert caps.supports_thinking is True
    assert caps.context_length == 131072
    assert chat.show_calls == ["gemma4:e2b"]


async def test_capabilities_tolerate_object_payload(tmp_path):
    chat = FakeChatClient()
    chat.show_response = SimpleNamespace(
        capabilities=["completion", "vision"], model_info={"gemma4.context_length": 8192}
    )
    provider = _provider(tmp_path, chat=chat)
    caps = await provider.capabilities()
    assert caps.exists is True
    assert caps.supports_vision is True
    assert caps.supports_thinking is False
    assert caps.context_length == 8192


async def test_capabilities_result_is_cached_but_refreshable(tmp_path):
    chat = FakeChatClient()
    chat.show_response = {"capabilities": ["completion"], "model_info": {}}
    provider = _provider(tmp_path, chat=chat)
    await provider.capabilities()
    await provider.capabilities()
    assert len(chat.show_calls) == 1
    await provider.capabilities(force_refresh=True)
    assert len(chat.show_calls) == 2


async def test_missing_model_is_reported_and_not_cached(tmp_path):
    chat = FakeChatClient()
    chat.show_error = ResponseError("model 'gemma4:e2b' not found", 404)
    provider = _provider(tmp_path, chat=chat)
    caps = await provider.capabilities()
    assert caps.exists is False
    assert caps.error == "model not installed"
    await provider.capabilities()
    assert len(chat.show_calls) == 2  # retried: a fresh install should show up


async def test_capabilities_server_down_is_reported(tmp_path):
    chat = FakeChatClient()
    chat.show_error = httpx.ConnectError("connection refused")
    provider = _provider(tmp_path, chat=chat)
    caps = await provider.capabilities()
    assert caps.exists is False
    assert (caps.error or "").startswith("server unreachable")


# --------------------------------------------------------------- strip helpers


def test_strip_thinking_removes_thought_blocks_and_prefix():
    assert strip_thinking("<|channel>thought hidden reasoning<channel|>{\"a\": 1}") == '{"a": 1}'
    assert strip_thinking("<|think|> leading junk") == "leading junk"
    assert strip_thinking("plain text") == "plain text"
    assert strip_thinking("") == ""
