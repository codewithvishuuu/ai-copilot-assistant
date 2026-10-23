"""Authorization checks: guild/channel allowlists and moderator/dev gating.

Pure functions take primitives (ids, permission-like objects) so they are unit
testable without Discord. The ``ensure_*`` helpers add ephemeral Discord replies.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from ..errors import NotAllowedError, NotDevUserError, NotModeratorError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- pure logic


def guild_allowed(allowed_guild_ids: Sequence[int], guild_id: int | None) -> bool:
    """Empty allowlist denies everything (safe default). DMs are not supported."""
    return guild_id is not None and guild_id in set(allowed_guild_ids)


def channel_allowed(
    allowed_channel_ids: Sequence[int],
    channel_id: int | None,
    parent_id: int | None = None,
) -> bool:
    """Empty allowlist allows every channel inside an allowed guild.

    A thread matches when either the thread id itself or its parent channel id
    is on the allowlist.
    """
    if not allowed_channel_ids:
        return True
    allowed = set(allowed_channel_ids)
    return (channel_id is not None and channel_id in allowed) or (
        parent_id is not None and parent_id in allowed
    )


def target_thread_ids(thread: object) -> tuple[int | None, int | None, int | None]:
    """Return (guild_id, thread_id, parent_id) from a target thread, defensively."""
    guild = getattr(thread, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        guild_id = getattr(thread, "guild_id", None)
    return guild_id, getattr(thread, "id", None), getattr(thread, "parent_id", None)


def target_thread_allowed(
    allowed_guild_ids: Sequence[int],
    allowed_channel_ids: Sequence[int],
    interaction_guild_id: int | None,
    target_guild_id: int | None,
    target_channel_id: int | None,
    target_parent_id: int | None,
) -> bool:
    """An explicit target must be in the interaction guild and pass the channel allowlist."""
    try:
        interaction_guild = int(interaction_guild_id) if interaction_guild_id is not None else None
        target_guild = int(target_guild_id) if target_guild_id is not None else None
        channel_id = int(target_channel_id) if target_channel_id is not None else None
        parent_id = int(target_parent_id) if target_parent_id is not None else None
    except (TypeError, ValueError):
        return False
    if interaction_guild is None or target_guild is None or target_guild != interaction_guild:
        return False
    if channel_id is None:
        return False
    if not guild_allowed(allowed_guild_ids, target_guild):
        return False
    return channel_allowed(allowed_channel_ids, channel_id, parent_id)


def is_moderator(
    permissions: object | None,
    role_ids: Iterable[int],
    moderator_role_ids: Sequence[int],
) -> bool:
    """Moderator = Discord Manage Threads/Manage Messages permission OR allowlisted role."""
    if permissions is not None and (
        getattr(permissions, "manage_threads", False) or getattr(permissions, "manage_messages", False)
    ):
        return True
    return bool(set(role_ids) & set(moderator_role_ids))


def is_dev_user(dev_user_ids: Sequence[int], user_id: int | None) -> bool:
    return user_id is not None and user_id in set(dev_user_ids)


# ------------------------------------------------------------- Discord helpers


async def _deny(interaction, message: str) -> None:
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except Exception:  # pragma: no cover - defensive: interaction may be unusable
        log.warning("Could not send denial message for interaction")


def interaction_ids(interaction) -> tuple[int | None, int | None, int | None]:
    """Return (guild_id, channel_id, parent_id) from a Discord interaction, defensively."""
    guild_id = getattr(interaction, "guild_id", None)
    channel = getattr(interaction, "channel", None)
    channel_id = getattr(channel, "id", None) or getattr(interaction, "channel_id", None)
    parent_id = getattr(channel, "parent_id", None)
    return guild_id, channel_id, parent_id


async def ensure_allowed(interaction, settings) -> bool:
    """Deny and return False when the interaction is outside the allowlists."""
    guild_id, channel_id, parent_id = interaction_ids(interaction)
    if not guild_allowed(settings.guild_allowlist, guild_id):
        await _deny(interaction, str(NotAllowedError()))
        return False
    if not channel_allowed(settings.channel_allowlist, channel_id, parent_id):
        await _deny(interaction, str(NotAllowedError()))
        return False
    return True


async def ensure_target_thread(interaction, thread, settings) -> bool:
    """Deny a target thread outside the interaction guild or channel allowlist."""
    interaction_guild_id, _channel_id, _parent_id = interaction_ids(interaction)
    target_guild_id, target_channel_id, target_parent_id = target_thread_ids(thread)
    if target_thread_allowed(
        settings.guild_allowlist,
        settings.channel_allowlist,
        interaction_guild_id,
        target_guild_id,
        target_channel_id,
        target_parent_id,
    ):
        return True
    log.info(
        "Target thread check denied (guild=%s channel=%s)",
        target_guild_id,
        target_channel_id,
    )
    await _deny(interaction, str(NotAllowedError()))
    return False


async def ensure_moderator(interaction, settings) -> bool:
    """Deny and return False when the requester lacks moderator rights."""
    member = getattr(interaction, "user", None)
    permissions = getattr(member, "guild_permissions", None)
    roles = [r.id for r in getattr(member, "roles", []) if hasattr(r, "id")]
    user_id = getattr(member, "id", None)
    if is_moderator(permissions, roles, settings.moderator_role_ids):
        return True
    log.info("Moderator check denied for user_id=%s", user_id)
    await _deny(interaction, str(NotModeratorError()))
    return False


async def ensure_dev_user(interaction, settings) -> bool:
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if is_dev_user(settings.dev_user_ids, user_id):
        return True
    await _deny(interaction, str(NotDevUserError()))
    return False
