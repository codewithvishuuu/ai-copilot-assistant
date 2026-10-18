"""Authorization: allowlists, moderator/dev gates, and their Discord-facing helpers."""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeInteraction, fake_settings, make_user

from copilot.ui.checks import (
    channel_allowed,
    ensure_allowed,
    ensure_dev_user,
    ensure_moderator,
    guild_allowed,
    is_dev_user,
    is_moderator,
)

# ------------------------------------------------------------------ pure logic


def test_guild_allowlist_empty_denies_everything():
    assert guild_allowed([], 5) is False
    assert guild_allowed([], None) is False
    assert guild_allowed([5], 5) is True
    assert guild_allowed([5], 6) is False
    assert guild_allowed([5], None) is False


def test_channel_allowlist_empty_allows_all():
    assert channel_allowed([], 1) is True
    assert channel_allowed([], None) is True


def test_channel_allowlist_matches_channel_or_parent():
    assert channel_allowed([2], 2) is True
    assert channel_allowed([42], 999, parent_id=42) is True
    assert channel_allowed([2], 7, parent_id=8) is False
    assert channel_allowed([2], None, parent_id=None) is False


def test_moderator_by_permission_or_role():
    perms = SimpleNamespace(manage_threads=True, manage_messages=False)
    assert is_moderator(perms, [], []) is True
    assert is_moderator(None, [9], [9]) is True
    assert is_moderator(None, [8], [9]) is False
    assert is_moderator(None, [], []) is False
    assert is_moderator(SimpleNamespace(manage_threads=False, manage_messages=False), [], []) is False


def test_dev_user_gate():
    assert is_dev_user([7], 7) is True
    assert is_dev_user([7], 8) is False
    assert is_dev_user([], 7) is False
    assert is_dev_user([7], None) is False


# --------------------------------------------------------------- ensure helpers


async def test_ensure_allowed_denies_unlisted_guild(tmp_path):
    settings = fake_settings(tmp_path, guild_allowlist=[500])
    interaction = FakeInteraction(guild_id=999)
    assert await ensure_allowed(interaction, settings) is False
    assert interaction.response.sent, "a denial message should have been sent"
    assert "not enabled" in interaction.response.sent[0]["content"]


async def test_ensure_allowed_accepts_allowlisted_guild(tmp_path):
    settings = fake_settings(tmp_path, guild_allowlist=[500], channel_allowlist=[])
    interaction = FakeInteraction(guild_id=500, channel_id=2002)
    assert await ensure_allowed(interaction, settings) is True
    assert interaction.response.sent == []


async def test_ensure_allowed_channel_allowlist_via_parent(tmp_path):
    settings = fake_settings(tmp_path, guild_allowlist=[500], channel_allowlist=[42])
    denied = FakeInteraction(guild_id=500, channel_id=7777, parent_id=None)
    assert await ensure_allowed(denied, settings) is False
    allowed = FakeInteraction(guild_id=500, channel_id=7777, parent_id=42)
    assert await ensure_allowed(allowed, settings) is True


async def test_ensure_moderator(tmp_path):
    settings = fake_settings(tmp_path, moderator_role_ids=[9])

    plain = FakeInteraction(user=make_user(7))
    assert await ensure_moderator(plain, settings) is False
    assert "moderator" in plain.response.sent[0]["content"].lower()

    by_role = FakeInteraction(user=make_user(7, roles=(9,)))
    assert await ensure_moderator(by_role, settings) is True

    by_perm = FakeInteraction(user=make_user(7, manage_threads=True))
    assert await ensure_moderator(by_perm, settings) is True


async def test_ensure_dev_user(tmp_path):
    settings = fake_settings(tmp_path, dev_user_ids=[7])

    assert await ensure_dev_user(FakeInteraction(user=make_user(7)), settings) is True
    denied = FakeInteraction(user=make_user(8))
    assert await ensure_dev_user(denied, settings) is False
    assert "developer" in denied.response.sent[0]["content"].lower()
