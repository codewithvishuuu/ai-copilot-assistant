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
