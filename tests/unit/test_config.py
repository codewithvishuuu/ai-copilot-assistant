"""Configuration: required fields, list parsing, bounds, secret hygiene."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.config import Settings
from copilot.errors import ConfigError


def test_missing_token_fails_startup_validation():
    settings = Settings(_env_file=None)
    assert settings.token == ""
    with pytest.raises(ConfigError) as excinfo:
        settings.validate_for_run()
    assert "DISCORD_TOKEN" in str(excinfo.value)


def test_id_lists_parse_comma_separated():
    settings = Settings(
        _env_file=None,
        guild_allowlist="111, 222 ,333",
        channel_allowlist="",
        moderator_role_ids="9",
        dev_user_ids="1,2",
    )
    assert settings.guild_allowlist == [111, 222, 333]
    assert settings.channel_allowlist == []
    assert settings.moderator_role_ids == [9]
    assert settings.dev_user_ids == [1, 2]


def test_invalid_id_list_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, guild_allowlist="abc")


def test_negative_or_zero_ids_are_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, guild_allowlist="0")


def test_cross_field_check_max_messages():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_messages=100, hard_max_messages=50)


def test_cross_field_check_conversation_stored_turns():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            conversation_max_turns=10,
            conversation_max_stored_turns=19,  # must be at least 2 x max_turns
        )
    Settings(_env_file=None, conversation_max_turns=10, conversation_max_stored_turns=20)


def test_conversation_retention_defaults_and_bounds():
    defaults = Settings(_env_file=None)
    assert defaults.conversation_retention_days == 7
    assert defaults.conversation_max_stored_turns == 40
    with pytest.raises(ValidationError):
        Settings(_env_file=None, conversation_retention_days=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, conversation_max_stored_turns=1)


def test_conversation_grounding_defaults_and_bounds():
    defaults = Settings(_env_file=None)
    assert defaults.conversation_grounding_enabled is True
    assert defaults.conversation_grounding_top_k == 2
    assert defaults.conversation_grounding_max_chars == 1200
    for bad in (0, 6):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, conversation_grounding_top_k=bad)
    for bad in (199, 4001):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, conversation_grounding_max_chars=bad)
    low = Settings(_env_file=None, conversation_grounding_top_k=1, conversation_grounding_max_chars=200)
    assert low.conversation_grounding_top_k == 1
    assert low.conversation_grounding_max_chars == 200
    high = Settings(_env_file=None, conversation_grounding_top_k=5, conversation_grounding_max_chars=4000)
    assert high.conversation_grounding_top_k == 5
    assert high.conversation_grounding_max_chars == 4000


def test_conversation_grounding_env_overrides(monkeypatch):
    monkeypatch.setenv("CONVERSATION_GROUNDING_ENABLED", "false")
    monkeypatch.setenv("CONVERSATION_GROUNDING_TOP_K", "3")
    monkeypatch.setenv("CONVERSATION_GROUNDING_MAX_CHARS", "900")
    settings = Settings(_env_file=None)
    assert settings.conversation_grounding_enabled is False
    assert settings.conversation_grounding_top_k == 3
    assert settings.conversation_grounding_max_chars == 900


def test_content_studio_defaults_and_bounds():
    defaults = Settings(_env_file=None)
    assert defaults.content_generation_max_chars == 1800
    assert defaults.content_generation_timeout_s == 120.0
    assert defaults.content_generation_max_tokens == 1024
    assert defaults.content_approval_ttl_s == 3600
    assert defaults.content_context_max_chars == 2000
    assert defaults.content_context_top_k == 3
    assert defaults.content_publishing_enabled is False

    for bad in (99, 2001):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, content_generation_max_chars=bad)
    for bad in (59, 604801):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, content_approval_ttl_s=bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, content_context_top_k=6)
    assert Settings(_env_file=None, content_context_top_k=0).content_context_top_k == 0
    assert Settings(_env_file=None, content_publishing_enabled=True).content_publishing_enabled


def test_ollama_concurrency_and_chat_waiting_defaults_and_bounds():
    defaults = Settings(_env_file=None)
    assert defaults.ollama_max_concurrent == 2
    assert defaults.conversation_max_waiting == 2
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_max_concurrent=0)  # a bound of zero makes no sense
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_max_concurrent=9)  # never more than the cap
    with pytest.raises(ValidationError):
        Settings(_env_file=None, conversation_max_waiting=-1)
    assert Settings(_env_file=None, conversation_max_waiting=0).conversation_max_waiting == 0


def test_numeric_bounds_enforced():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_num_ctx=1)  # below minimum
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="VERBOSE")


def test_secret_never_appears_in_repr():
    settings = Settings(_env_file=None, discord_token="super-secret-token-value")
    assert "super-secret-token-value" not in repr(settings)
    assert settings.token == "super-secret-token-value"


def test_environment_variable_overrides(monkeypatch):
    monkeypatch.setenv("PER_USER_COOLDOWN_S", "7")
    monkeypatch.setenv("OLLAMA_MODEL", "test:model")
    settings = Settings(_env_file=None)
    assert settings.per_user_cooldown_s == 7
    assert settings.ollama_model == "test:model"


def test_empty_optional_int_becomes_none(monkeypatch):
    monkeypatch.setenv("TEST_GUILD_ID", "")
    settings = Settings(_env_file=None)
    assert settings.test_guild_id is None


def test_db_path_and_dirs(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "d", db_filename="x.db")
    assert settings.db_path == tmp_path / "d" / "x.db"
    assert settings.logs_dir == tmp_path / "d" / "logs"


def test_whitespace_token_rejected():
    settings = Settings(_env_file=None, discord_token=" token-with-space")
    with pytest.raises(ConfigError):
        settings.validate_for_run()


def test_unused_settings_are_gone_and_confidence_default_is_live():
    fields = Settings.model_fields
    assert "debug_log_content" not in fields
    assert "moderation_report_channel_id" not in fields
    assert Settings(_env_file=None).moderation_default_confidence == 0.75
    with pytest.raises(ValidationError):
        Settings(_env_file=None, moderation_default_confidence=1.5)
