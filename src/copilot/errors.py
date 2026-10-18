"""Typed application errors with friendly user-facing messages.

Rules:
- Anything shown to a Discord user must come from here (never a raw traceback).
- Details (for local logs only) go into ``.detail``, never into user messages.
"""

from __future__ import annotations

import math
import secrets


def new_error_code() -> str:
    """Short correlation code shown to users and written to local logs."""
    return "CC-" + secrets.token_hex(2).upper()


class CopilotError(Exception):
    """Base class: message text is safe to show to end users."""

    default_message = "Something went wrong while processing the request."

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.detail = detail

    def user_message(self) -> str:
        return str(self)


# --------------------------------------------------------------------- startup


class ConfigError(Exception):
    """Raised during startup when configuration is missing or invalid."""


# -------------------------------------------------------------------- provider


class ProviderUnavailableError(CopilotError):
    default_message = (
        "The local AI server (Ollama) is not reachable. "
        "Start Ollama on the machine running this bot and try again."
    )


class ModelNotInstalledError(CopilotError):
    default_message = (
        "The configured AI model is not installed in Ollama. "
        "Ask the bot operator to run: ollama pull <model>"
    )


class ProviderTimeoutError(CopilotError):
    default_message = (
        "The local AI model took too long to respond. "
        "Try again, or ask the operator to check the machine's load."
    )


class ProviderError(CopilotError):
    default_message = "The local AI model returned an error. Please try again later."


# ---------------------------------------------------------------------- output


class OutputValidationError(CopilotError):
    default_message = (
        "The AI model did not produce a usable result for this request. "
        "Nothing was saved. You can try again."
    )


class EmptyModelOutputError(OutputValidationError):
    default_message = (
        "The AI model returned an empty response (a known small-model failure mode). "
        "Please try again."
    )


# ------------------------------------------------------------------ permission


class NotAllowedError(CopilotError):
    default_message = "Community Copilot is not enabled in this server or channel."


class NotModeratorError(CopilotError):
    default_message = "This command requires moderator permissions."


class NotDevUserError(CopilotError):
    default_message = "This command is restricted to the bot's developer."


# ------------------------------------------------------------------- scheduling


class QueueFullError(CopilotError):
    default_message = "The bot is busy with other requests right now. Please try again in a minute."


class CooldownError(CopilotError):
    default_message = "Please wait a moment before using this command again."

    def __init__(self, retry_after_s: float, *, detail: str | None = None) -> None:
        wait = max(1, math.ceil(retry_after_s))
        super().__init__(
            f"Please wait about {wait} second{'s' if wait != 1 else ''} before trying again.",
            detail=detail,
        )
        self.retry_after_s = retry_after_s


class GuildLimitError(CopilotError):
    default_message = (
        "This server has reached its hourly request limit for Community Copilot. "
        "Please try again later."
    )

    def __init__(self, retry_after_s: float, *, detail: str | None = None) -> None:
        super().__init__(
            f"This server has reached its hourly limit. Try again in about "
            f"{max(1, math.ceil(retry_after_s / 60))} minute(s).",
            detail=detail,
        )
        self.retry_after_s = retry_after_s


class JobCancelledError(CopilotError):
    default_message = "Request cancelled."


class JobTimedOutError(ProviderTimeoutError):
    default_message = (
        "This request took too long and was stopped. "
        "Try again with a smaller thread or a smaller message count."
    )


# --------------------------------------------------------------------- discord


class ThreadInaccessibleError(CopilotError):
    default_message = (
        "I can't read that thread. Make sure it's a forum post or thread in this server "
        "and that I have permission to view its history."
    )


class EmptyThreadError(CopilotError):
    default_message = "That thread has no usable messages to summarize."


# -------------------------------------------------------------------- storage


class DatabaseError(CopilotError):
    default_message = "The local database had a problem. Nothing was saved; please try again."


class VisionNotSupportedError(CopilotError):
    default_message = "Image analysis is not available right now; the summary will use text only."
