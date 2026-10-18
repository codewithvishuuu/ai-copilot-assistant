"""Log redaction: registered secrets, token shapes, key=value patterns, tracebacks."""

from __future__ import annotations

import logging

from copilot.util.logging import REDACTED, RedactingFormatter, redact, register_secret

# Shape-compatible with a Discord bot token but NOT a real one.
FAKE_TOKEN_SHAPE = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0.AbCdEf.abcdefghijklmnopqrstuvwxyz0123456"


def test_registered_secret_is_redacted():
    register_secret("my-hidden-value-123")
    assert redact("token is my-hidden-value-123 ok") == f"token is {REDACTED} ok"


def test_short_secrets_are_not_registered():
    register_secret("short")
    assert redact("short") == "short"


def test_token_shaped_string_is_redacted_even_unregistered():
    out = redact(f"Authorization: Bot {FAKE_TOKEN_SHAPE} (end)")
    assert FAKE_TOKEN_SHAPE not in out
    assert REDACTED in out


def test_key_value_patterns_are_redacted():
    assert redact("token=abc123") == f"token={REDACTED}"
    assert redact("password: hunter2") == f"password: {REDACTED}"
    assert redact("API_key = zzzz") == f"API_key = {REDACTED}"


def test_formatter_redacts_message_and_traceback():
    register_secret("formatter-secret-value")
    formatter = RedactingFormatter("%(message)s")

    record = logging.LogRecord(
        "copilot.test", logging.ERROR, __file__, 1, "using formatter-secret-value", None, None
    )
    assert "formatter-secret-value" not in formatter.format(record)
    assert REDACTED in formatter.format(record)

    try:
        raise ValueError(f"boom with {FAKE_TOKEN_SHAPE}")
    except ValueError:
        import sys

        exc_record = logging.LogRecord(
            "copilot.test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
        formatted = formatter.format(exc_record)
        assert FAKE_TOKEN_SHAPE not in formatted
        assert "ValueError" in formatted


def test_empty_input_is_untouched():
    assert redact("") == ""
