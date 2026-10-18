"""Typed configuration loaded from the environment / `.env` (pydantic-settings).

Security notes:
- ``discord_token`` is a ``SecretStr``: it never appears in reprs or logs.
- ID lists are comma-separated in ``.env``; empty means "deny everything" for
  guilds and "allow all channels" for channels (see ui/checks.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .errors import ConfigError

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _split_ids(raw: str, *, field_name: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:  # pragma: no cover - message tested via config test
            raise ValueError(
                f"{field_name} must be comma-separated Discord IDs (digits only), got {part!r}"
            ) from exc
        if value <= 0:
            raise ValueError(f"{field_name}: IDs must be positive integers, got {part!r}")
        ids.append(value)
    return ids


class Settings(BaseSettings):
    """All runtime configuration. Construct with ``Settings()`` (reads .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ----- Discord -----
    discord_token: SecretStr = SecretStr("")
    guild_allowlist: Annotated[list[int], NoDecode] = []
    channel_allowlist: Annotated[list[int], NoDecode] = []
    test_guild_id: int | None = None
    moderator_role_ids: Annotated[list[int], NoDecode] = []
    dev_user_ids: Annotated[list[int], NoDecode] = []

    # ----- Ollama -----
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e2b"
    ollama_num_ctx: int = Field(default=4096, ge=256, le=131072)
    ollama_request_timeout_s: float = Field(default=180.0, gt=0)
    ollama_keep_alive: str = "5m"
    ollama_max_tokens: int = Field(default=1024, ge=16, le=8192)
    model_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    thinking_enabled: bool = False
    vision_enabled: bool = True

    # ----- Collection limits -----
    max_messages: int = Field(default=150, ge=5)
    hard_max_messages: int = Field(default=500, ge=5)
    max_chars: int = Field(default=24000, ge=1000)
    max_images: int = Field(default=4, ge=0)
    max_image_bytes: int = Field(default=6_000_000, ge=1000)

    # ----- Queue and rate limits -----
    queue_capacity: int = Field(default=5, ge=1)
    per_user_cooldown_s: int = Field(default=60, ge=0)
    per_guild_hourly_limit: int = Field(default=20, ge=1)
    job_timeout_s: float = Field(default=300.0, gt=0)
    fetch_timeout_s: float = Field(default=60.0, gt=0)

    # ----- Storage, privacy, logging -----
    data_dir: Path = Path("data")
    db_filename: str = "copilot.db"
    retention_days: int = Field(default=90, ge=1)
    log_level: str = "INFO"
    debug_log_content: bool = False
    ephemeral_by_default: bool = True

    # ----- Insights -----
    insights_min_threads: int = Field(default=5, ge=1)
    insights_window_days: int = Field(default=30, ge=1)

    # ------------------------------------------------------------- validators

    @field_validator(
        "guild_allowlist",
        "channel_allowlist",
        "moderator_role_ids",
        "dev_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_id_lists(cls, value: object, info) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return _split_ids(value, field_name=str(info.field_name).upper())
        return value

    @field_validator("test_guild_id", mode="before")
    @classmethod
    def _empty_test_guild_is_none(cls, value: object) -> object:
        if value == "" or value is None:
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}, got {value!r}")
        return level

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Settings:
        if self.max_messages > self.hard_max_messages:
            raise ValueError("MAX_MESSAGES cannot exceed HARD_MAX_MESSAGES")
        return self

    # ------------------------------------------------------------- properties

    @property
    def token(self) -> str:
        return self.discord_token.get_secret_value()

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_filename

    @property
    def logs_dir(self) -> Path:
        return Path(self.data_dir) / "logs"

    def validate_for_run(self) -> None:
        """Startup gate: raise ``ConfigError`` with a helpful, secret-free message."""
        if not self.token.strip():
            raise ConfigError(
                "DISCORD_TOKEN is not set. Create a bot application, then put its token "
                "in a local .env file (see .env.example). Never share the token in chat."
            )
        if self.token.strip() != self.token:
            raise ConfigError("DISCORD_TOKEN contains leading/trailing whitespace — check your .env.")
