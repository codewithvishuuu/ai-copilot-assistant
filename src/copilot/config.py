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
    # Hard cap on simultaneous Ollama generation calls (queue job + chat by
    # default). Lower to 1 on very small machines to serialize everything.
    ollama_max_concurrent: int = Field(default=2, ge=1, le=8)
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

    # ----- Conversation (mention-triggered chat) -----
    conversation_enabled: bool = True
    conversation_max_turns: int = Field(default=6, ge=1, le=50)
    conversation_max_chars: int = Field(default=6000, ge=500, le=32000)
    conversation_ttl_s: int = Field(default=3600, ge=60)
    conversation_cooldown_s: int = Field(default=10, ge=0)
    conversation_timeout_s: float = Field(default=120.0, gt=0)
    conversation_max_tokens: int = Field(default=512, ge=16, le=4096)
    # Phase 2 persistence: turns are stored in SQLite, scoped per
    # (guild, channel, user); age-bounded by the daily sweep and row-bounded
    # per scope on every write.
    conversation_retention_days: int = Field(default=7, ge=1, le=90)
    conversation_max_stored_turns: int = Field(default=40, ge=2, le=200)
    # Chat requests allowed to WAIT for the single in-flight chat generation;
    # anything beyond this is rejected with a rate-limited notice instead of
    # piling up unbounded typing/waiting tasks.
    conversation_max_waiting: int = Field(default=2, ge=0, le=10)

    # Phase 4 KB grounding (design D22): an optional, bounded knowledge-base
    # block built only from moderator-approved documents, retrieved by the
    # chat cog via the repository's own FTS search. Dropped before history
    # when the prompt budget is tight — continuity wins.
    conversation_grounding_enabled: bool = True
    conversation_grounding_top_k: int = Field(default=2, ge=1, le=5)
    conversation_grounding_max_chars: int = Field(default=1200, ge=200, le=4000)

    # ----- Storage, privacy, logging -----
    data_dir: Path = Path("data")
    db_filename: str = "copilot.db"
    retention_days: int = Field(default=90, ge=1)
    log_level: str = "INFO"
    ephemeral_by_default: bool = True

    # ----- Insights -----
    insights_min_threads: int = Field(default=5, ge=1)
    insights_window_days: int = Field(default=30, ge=1)

    # ----- Missions (Phase 3) -----
    # A mission is a bounded, human-started plan over the existing read-only
    # skill registry. Steps never write, publish or execute external actions.
    missions_enabled: bool = True
    mission_max_steps: int = Field(default=4, ge=1, le=6)
    mission_max_goal_chars: int = Field(default=500, ge=20, le=1000)
    mission_step_timeout_s: float = Field(default=45.0, gt=0)
    mission_max_result_chars: int = Field(default=8000, ge=500, le=20000)

    # ----- Main Host Agent (gap closure) -----
    # The host delegates only to registered read-only skills with bounded
    # concurrency. It never publishes or performs moderation actions.
    agent_enabled: bool = True
    agent_max_steps: int = Field(default=5, ge=1, le=6)
    agent_max_goal_chars: int = Field(default=500, ge=20, le=800)
    agent_max_parallel: int = Field(default=2, ge=1, le=4)
    agent_run_timeout_s: float = Field(default=150.0, gt=0, le=600)

    # ----- Attachment ingestion (gap closure) -----
    attachments_enabled: bool = True
    attachment_max_count: int = Field(default=5, ge=1, le=10)
    attachment_max_bytes: int = Field(default=8_000_000, ge=100_000, le=25_000_000)
    attachment_max_text_chars: int = Field(default=16_000, ge=500, le=60_000)
    attachment_max_pdf_pages: int = Field(default=30, ge=1, le=100)
    attachment_max_image_pixels: int = Field(default=20_000_000, ge=10_000)
    attachment_retention_days: int = Field(default=3, ge=1, le=30)
    attachment_followup_turns: int = Field(default=6, ge=1, le=20)

    # ----- AI moderation (gap closure) -----
    # Disabled by default; enabling requires an explicit per-guild policy and a
    # private moderator report channel. No auto-ban/timeout exists at all.
    moderation_enabled: bool = False
    moderation_repeat_window_days: int = Field(default=7, ge=1, le=90)
    moderation_timeout_s: float = Field(default=30.0, gt=0, le=120)
    moderation_max_tokens: int = Field(default=300, ge=64, le=1024)
    moderation_default_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    moderation_default_severity: str = "high"

    # ----- Durable scheduler (gap closure) -----
    scheduler_enabled: bool = True
    scheduler_tick_s: float = Field(default=30.0, ge=5.0, le=300.0)
    scheduler_tick_batch: int = Field(default=10, ge=1, le=50)
    scheduler_job_timeout_s: float = Field(default=120.0, gt=0, le=600)
    scheduler_max_attempts: int = Field(default=3, ge=1, le=10)

    # ----- Chat reply approval (gap closure) -----
    # When enabled, generated chat replies are queued as exact content+channel
    # approvals and are not sent until a different human moderator approves.
    reply_approval_enabled: bool = False
    reply_approval_ttl_s: int = Field(default=3600, ge=60, le=604800)

    # ----- Content voice memory (gap closure) -----
    content_feedback_limit: int = Field(default=6, ge=0, le=20)
    content_similarity_threshold: float = Field(default=0.82, ge=0.5, le=0.99)
    content_dedupe_recent: int = Field(default=40, ge=5, le=200)

    # ----- Web research (Phase 3) -----
    # Off unless an operator configures a provider and key. When unavailable,
    # research degrades to a clear status, never to fabricated citations.
    web_research_enabled: bool = False
    web_search_provider: str = "none"
    brave_search_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    web_search_timeout_s: float = Field(default=20.0, gt=0)
    web_search_max_results: int = Field(default=5, ge=1, le=8)
    web_search_max_chars: int = Field(default=6000, ge=500, le=20000)
    research_max_sources: int = Field(default=5, ge=1, le=8)
    research_synthesis_max_tokens: int = Field(default=600, ge=64, le=2000)

    # ----- Content planning (Phase 3/4) -----
    content_calendar_max_entries: int = Field(default=100, ge=1, le=500)
    contextual_reply_max_chars: int = Field(default=800, ge=100, le=1800)
    event_plan_max_chars: int = Field(default=1500, ge=100, le=4000)

    # ----- Content Studio (Phase 2B) -----
    # Generation is bounded; publication is a separate, human-approved action.
    # Publishing is off by default so an operator must opt in after configuring
    # a server/channel where the bot may post.
    content_generation_max_chars: int = Field(default=1800, ge=100, le=2000)
    content_generation_timeout_s: float = Field(default=120.0, gt=0)
    content_generation_max_tokens: int = Field(default=1024, ge=64, le=4096)
    content_approval_ttl_s: int = Field(default=3600, ge=60, le=604800)
    content_context_max_chars: int = Field(default=2000, ge=0, le=8000)
    content_context_top_k: int = Field(default=3, ge=0, le=5)
    content_publishing_enabled: bool = False

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

    @field_validator("web_search_provider")
    @classmethod
    def _normalize_research_provider(cls, value: str) -> str:
        provider = (value or "none").strip().lower()
        if provider not in {"none", "brave", "tavily"}:
            raise ValueError("WEB_SEARCH_PROVIDER must be one of: none, brave, tavily")
        return provider

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Settings:
        if self.max_messages > self.hard_max_messages:
            raise ValueError("MAX_MESSAGES cannot exceed HARD_MAX_MESSAGES")
        if self.conversation_max_stored_turns < self.conversation_max_turns * 2:
            raise ValueError(
                "CONVERSATION_MAX_STORED_TURNS must be at least twice "
                "CONVERSATION_MAX_TURNS, or stored history could never fill the prompt"
            )
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

    @property
    def web_search_api_key(self) -> str:
        if self.web_search_provider == "brave":
            return self.brave_search_api_key.get_secret_value().strip()
        if self.web_search_provider == "tavily":
            return self.tavily_api_key.get_secret_value().strip()
        return ""

    @property
    def research_unavailable_reason(self) -> str | None:
        """Why live web research cannot run, or None when it is ready."""
        if not self.web_research_enabled:
            return "Web research is disabled (WEB_RESEARCH_ENABLED=false)."
        if self.web_search_provider == "none":
            return "No web search provider is configured (WEB_SEARCH_PROVIDER=none)."
        if not self.web_search_api_key:
            return (
                f"WEB_SEARCH_PROVIDER={self.web_search_provider} is set but its API key is missing."
            )
        return None

    def validate_for_run(self) -> None:
        """Startup gate: raise ``ConfigError`` with a helpful, secret-free message."""
        if not self.token.strip():
            raise ConfigError(
                "DISCORD_TOKEN is not set. Create a bot application, then put its token "
                "in a local .env file (see .env.example). Never share the token in chat."
            )
        if self.token.strip() != self.token:
            raise ConfigError("DISCORD_TOKEN contains leading/trailing whitespace — check your .env.")
