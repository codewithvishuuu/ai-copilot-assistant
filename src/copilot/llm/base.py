"""Provider interfaces shared by all model backends.

The model is a text -> JSON transformer: no tools, no shell, no filesystem,
no network access beyond the configured local Ollama endpoint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict, runtime_checkable


class ChatMessage(TypedDict):
    """One entry of a multi-turn chat payload (roles: system/user/assistant)."""

    role: str
    content: str


@dataclass(frozen=True)
class ProviderHealth:
    reachable: bool
    version: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """What the *actual* configured model reports at runtime (never inferred from its name)."""

    exists: bool
    name: str
    supports_vision: bool = False
    supports_thinking: bool = False
    context_length: int | None = None
    error: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
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
        """Run one single-turn chat completion and return the raw text response."""
        ...

    async def chat_messages(
        self,
        messages: Sequence[ChatMessage],
        *,
        images: Sequence[bytes] = (),
        json_schema: dict | None = None,
        timeout_s: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Run a multi-turn chat completion and return the raw text response.

        ``images`` (when the model supports vision) attach to the last user message.
        """
        ...

    async def health(self) -> ProviderHealth:
        """Is the local model server reachable?"""
        ...

    async def capabilities(self, *, force_refresh: bool = False) -> ModelCapabilities:
        """Runtime capability check for the configured model."""
        ...
