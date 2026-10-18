"""Small TTL cache for runtime model-capability checks (`/api/show` is cheap but not free)."""

from __future__ import annotations

import time

from .base import ModelCapabilities


class CapabilityCache:
    def __init__(self, ttl_s: float = 300.0) -> None:
        self.ttl_s = ttl_s
        self._value: ModelCapabilities | None = None
        self._fetched_at: float = 0.0

    def get(self) -> ModelCapabilities | None:
        if self._value is None:
            return None
        if (time.monotonic() - self._fetched_at) > self.ttl_s:
            return None
        return self._value

    def set(self, value: ModelCapabilities, *, cache_errors: bool = False) -> None:
        if not value.exists and not cache_errors:
            # Uninstalled/unreachable: don't cache — the operator may install the
            # model at any moment, and /status should reflect reality quickly.
            self._value = None
            return
        self._value = value
        self._fetched_at = time.monotonic()

    def invalidate(self) -> None:
        self._value = None
        self._fetched_at = 0.0
