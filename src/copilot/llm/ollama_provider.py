"""Ollama provider: async client wrapper with timeouts, JSON output, thinking control.

Verified behaviors this implementation relies on (2026-10-18, Ollama 0.40.2, gemma4:e2b):
- ``/api/show`` reports capabilities: completion, vision, audio, tools, thinking.
- The python client's ``chat`` accepts ``think`` and ``format`` (a JSON schema dict
  or the string ``"json"``).
- With thinking left on, small models can burn the whole ``num_predict`` budget on
  thought tokens and return EMPTY content (reproduced twice in Phase 0). Therefore
  thinking is explicitly disabled unless the operator opts in via THINKING_ENABLED.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Sequence
from typing import Any

import httpx
from ollama import AsyncClient, ResponseError

from ..errors import (
    ModelNotInstalledError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ..util.logging import redact
from ..util.text import strip_control_tokens
from .base import ChatMessage, ModelCapabilities, ProviderHealth
from .capabilities import CapabilityCache

log = logging.getLogger(__name__)

# Thought blocks emitted by Gemma-family models: <|channel>thought ... <channel|>
_THOUGHT_BLOCK_RE = re.compile(r"<\|channel>thought.*?<channel\|>", re.DOTALL)
_THINK_PREFIX_RE = re.compile(r"^\s*<\|think\|>\s*")


def strip_thinking(text: str) -> str:
    """Remove any thought-block channel from a model response (belt and braces)."""
    text = _THOUGHT_BLOCK_RE.sub("", text or "")
    return _THINK_PREFIX_RE.sub("", text)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read an attribute from an object or a key from a mapping (client version tolerance)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class OllamaProvider:
    def __init__(
        self,
        settings,
        *,
        client: AsyncClient | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client if client is not None else AsyncClient(host=settings.ollama_base_url)
        self._owns_http = http_client is None
        self._http = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(
                base_url=settings.ollama_base_url, timeout=httpx.Timeout(6.0, connect=4.0)
            )
        )
        self._cap_cache = CapabilityCache()
        self._schema_supported: bool | None = None  # None = untested, then True/False
        # Hard bound on simultaneous generation calls (queue job + chat by
        # default). Health/capability probes are short and deliberately not
        # gated. See ARCHITECTURE.md for the fairness reasoning.
        self._generation_gate = asyncio.Semaphore(
            max(1, int(getattr(settings, "ollama_max_concurrent", 2)))
        )

    # ------------------------------------------------------------------ health

    async def health(self) -> ProviderHealth:
        try:
            response = await self._http.get("/api/version")
            response.raise_for_status()
            version = response.json().get("version")
            return ProviderHealth(reachable=True, version=version)
        except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
            return ProviderHealth(reachable=False, error=redact(str(exc))[:200])

    # ------------------------------------------------------------ capabilities

    async def capabilities(self, *, force_refresh: bool = False) -> ModelCapabilities:
        if not force_refresh:
            cached = self._cap_cache.get()
            if cached is not None:
                return cached
        caps = await self._fetch_capabilities()
        self._cap_cache.set(caps)
        return caps

    async def _fetch_capabilities(self) -> ModelCapabilities:
        model = self._settings.ollama_model
        try:
            info = await asyncio.wait_for(self._client.show(model), timeout=10.0)
        except ResponseError as exc:
            if getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower():
                return ModelCapabilities(exists=False, name=model, error="model not installed")
            return ModelCapabilities(exists=False, name=model, error=redact(str(exc))[:200])
        except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
            return ModelCapabilities(exists=False, name=model, error=f"server unreachable: {redact(str(exc))[:120]}")

        raw_caps = _get(info, "capabilities", None) or []
        capability_set = {str(c) for c in raw_caps}

        model_info: dict[str, Any] = _get(info, "model_info", None) or {}
        context_length: int | None = None
        for key, value in model_info.items():
            if str(key).endswith(".context_length") and isinstance(value, (int, float)):
                context_length = int(value)
                break

        return ModelCapabilities(
            exists=True,
            name=model,
            supports_vision="vision" in capability_set,
            supports_thinking="thinking" in capability_set,
            context_length=context_length,
        )

    # -------------------------------------------------------------------- chat

    async def chat(
        self,
        *,
        system: str,
        user: str,
        images: Sequence[bytes] = (),
        json_schema: dict | None = None,
        timeout_s: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """One single-turn chat completion. Returns raw text (thinking blocks stripped)."""
        messages: list[ChatMessage] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return await self.chat_messages(
            messages,
            images=images,
            json_schema=json_schema,
            timeout_s=timeout_s,
            max_tokens=max_tokens,
        )

    async def chat_messages(
        self,
        messages: Sequence[ChatMessage],
        *,
        images: Sequence[bytes] = (),
        json_schema: dict | None = None,
        timeout_s: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Multi-turn chat completion; ``images`` attach to the last user message.

        Generation calls share one semaphore (``OLLAMA_MAX_CONCURRENT``) so the
        bot never exceeds the configured number of simultaneous Ollama requests.
        Time spent waiting for a slot is not counted against ``timeout_s``.
        """
        timeout = timeout_s if timeout_s is not None else self._settings.ollama_request_timeout_s
        if not messages:
            raise ValueError("messages must not be empty")
        payload: list[dict[str, Any]] = [dict(message) for message in messages]
        if images:
            target = next(
                (i for i in range(len(payload) - 1, -1, -1) if payload[i].get("role") == "user"),
                None,
            )
            if target is None:
                raise ValueError("images require at least one user message")
            payload[target]["images"] = [base64.b64encode(img).decode("ascii") for img in images]

        options = {
            "temperature": self._settings.model_temperature,
            "num_ctx": self._settings.ollama_num_ctx,
            "num_predict": max_tokens if max_tokens is not None else self._settings.ollama_max_tokens,
            "top_p": 0.95,
            "top_k": 64,
        }

        async with self._generation_gate:
            return await self._generate(payload, options, json_schema, timeout_s=timeout)

    async def _generate(
        self,
        payload: list[dict[str, Any]],
        options: dict[str, Any],
        json_schema: dict | None,
        *,
        timeout_s: float,
    ) -> str:
        """One generation call (already holding a semaphore slot).

        The schema fallback recurses here — never into ``chat_messages`` — so a
        single slot is never acquired twice from the same task.
        """
        format_value: Any = None
        if json_schema is not None:
            format_value = json_schema if self._schema_supported is not False else "json"

        try:
            response = await asyncio.wait_for(
                self._client.chat(
                    model=self._settings.ollama_model,
                    messages=payload,
                    stream=False,
                    think=bool(self._settings.thinking_enabled),
                    format=format_value,
                    options=options,
                    keep_alive=self._settings.ollama_keep_alive,
                ),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError(detail=f"chat timed out after {timeout_s:.0f}s") from exc
        except ResponseError as exc:
            status = getattr(exc, "status_code", None)
            text = redact(str(exc))
            if status == 404 or "not found" in text.lower():
                raise ModelNotInstalledError(detail=text) from exc
            if json_schema is not None and self._schema_supported is None and format_value is json_schema:
                # Schema-constrained decoding rejected -> fall back to plain JSON mode once.
                log.info("Ollama rejected JSON-schema format; falling back to format='json'")
                self._schema_supported = False
                return await self._generate(payload, options, json_schema, timeout_s=timeout_s)
            raise ProviderError(detail=text) from exc
        except (httpx.HTTPError, ConnectionError) as exc:
            raise ProviderUnavailableError(detail=redact(str(exc))[:200]) from exc

        if json_schema is not None and self._schema_supported is None:
            self._schema_supported = True

        message = _get(response, "message")
        content = _get(message, "content") if message is not None else None
        if content is None:
            content = ""
        return strip_control_tokens(strip_thinking(str(content)))

    # ----------------------------------------------------------------- closing

    async def aclose(self) -> None:
        targets = []
        ollama_http = getattr(self._client, "_client", None)  # underlying httpx client
        if ollama_http is not None and hasattr(ollama_http, "aclose"):
            targets.append(ollama_http)
        if self._owns_http:
            targets.append(self._http)
        for target in targets:
            try:
                await target.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.debug("Error while closing HTTP resources", exc_info=True)
