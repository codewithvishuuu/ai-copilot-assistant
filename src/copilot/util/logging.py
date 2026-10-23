"""Secret-safe logging.

- ``register_secret`` registers values that must never appear in logs.
- ``RedactingFormatter`` scrubs registered secrets, token-shaped strings, and
  ``token=...``-style patterns from every formatted record (including tracebacks).
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

REDACTED = "[REDACTED]"

# Discord bot tokens: three base64url-ish parts separated by dots (first part usually starts with M/N).
_TOKEN_SHAPE_RE = re.compile(r"\b[MNO][A-Za-z\d_-]{20,}\.[\w-]{5,8}\.[\w-]{20,}\b")
# key=value / key: value patterns for common secret names.
_KV_SECRET_RE = re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\b(\s*[:=]\s*)(\S+)")

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a secret value to be redacted from all log output."""
    if value and len(value) >= 8:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """Test helper: forget all registered secrets."""
    _registered_secrets.clear()


def redact(text: str) -> str:
    """Return ``text`` with any known or token-shaped secrets replaced."""
    if not text:
        return text
    out = text
    for secret in _registered_secrets:
        out = out.replace(secret, REDACTED)
    out = _TOKEN_SHAPE_RE.sub(REDACTED, out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out


def mask_id(value: object) -> str:
    """Mask a Discord id for logs: keep only the last four digits (``…1234``)."""
    if value is None:
        return "?"
    text = str(value)
    return f"…{text[-4:]}" if text else "?"


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs secrets from the final formatted string (tracebacks included)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - stdlib name
        return redact(super().format(record))


def setup_logging(level: str = "INFO", logs_dir: Path | None = None) -> None:
    """Configure the root logger once. Safe to call again (level is updated)."""
    root = logging.getLogger()
    root.setLevel(level)
    if getattr(root, "_copilot_configured", False):
        return

    formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / "copilot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # discord.py is chatty at INFO for every event; keep it at WARNING unless debugging.
    logging.getLogger("discord").setLevel("WARNING" if level != "DEBUG" else "DEBUG")
    logging.getLogger("httpx").setLevel("WARNING")

    root._copilot_configured = True  # type: ignore[attr-defined]
