"""Discord adapter for Content Studio target checks, sends and reconciliation.

This is a UI/adapter boundary only. It never decides whether publication is
allowed; that remains the approval service's job. It does perform a fresh
permission check immediately before sending and classifies failures into
"definitely not sent" and "unknown/possibly sent" for the publisher.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from ..core.content_studio import ContentTarget
from ..core.publishing import SendAmbiguous, SendRejected
from . import checks

if TYPE_CHECKING:
    from ..bot import CommunityCopilot

log = logging.getLogger(__name__)


class DiscordContentSender:
    def __init__(self, bot: CommunityCopilot) -> None:
        self._bot = bot

    async def verify_actor(self, actor) -> bool:
        """Recheck the human's current moderator rights via REST before a send."""
        try:
            actor_id = int(actor.actor_id)
            guild = self._bot.get_guild(int(actor.guild_id))
            if guild is None:
                guild = await self._bot.fetch_guild(int(actor.guild_id))
            member = guild.get_member(actor_id)
            if member is None:
                member = await guild.fetch_member(actor_id)
            roles = [role.id for role in getattr(member, "roles", []) if hasattr(role, "id")]
            return checks.is_moderator(
                getattr(member, "guild_permissions", None),
                roles,
                self._bot.settings.moderator_role_ids,
            )
        except Exception:  # noqa: BLE001 - cannot prove current authority, deny
            log.warning("Could not revalidate moderator rights for a content action")
            return False

    async def resolve_target(self, guild_id: int, channel_id: int) -> ContentTarget:
        channel, guild = await self._resolve_channel(guild_id, channel_id)
        if channel is None or guild is None:
            return ContentTarget(
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                exists=False,
                can_send=False,
            )
        permissions = self._permissions(channel, guild)
        return ContentTarget(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            exists=True,
            can_send=bool(permissions is not None and permissions.view_channel and permissions.send_messages),
        )

    async def send_content(self, *, guild_id: int, channel_id: int, content: str, nonce: str) -> str:
        channel, guild = await self._resolve_channel(guild_id, channel_id)
        if channel is None or guild is None:
            raise SendRejected("target_unavailable", "The target channel is no longer available.")
        permissions = self._permissions(channel, guild)
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            raise SendRejected("missing_permission", "The bot cannot post in the target channel.")
        try:
            message = await channel.send(
                content,
                nonce=nonce,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.RateLimited) as exc:
            raise SendRejected(_error_code(exc)) from exc
        except discord.HTTPException as exc:
            status = int(getattr(exc, "status", 0) or 0)
            if 400 <= status < 500 and status not in (408, 429):
                raise SendRejected(f"http_{status}") from exc
            raise SendAmbiguous(f"Discord HTTP {status or 'error'}") from exc
        except (TimeoutError, OSError) as exc:
            raise SendAmbiguous(type(exc).__name__) from exc
        except Exception as exc:  # noqa: BLE001 - unknown sends must never be retried blindly
            raise SendAmbiguous(type(exc).__name__) from exc
        return str(message.id)

    async def find_content_message(self, *, guild_id: int, channel_id: int, content: str, nonce: str) -> str | None:
        channel, guild = await self._resolve_channel(guild_id, channel_id)
        if channel is None or guild is None:
            return None
        permissions = self._permissions(channel, guild)
        if permissions is None or not permissions.view_channel:
            return None
        if not permissions.read_message_history:
            return None
        bot_id = int(getattr(self._bot.user, "id", 0) or 0)
        async for message in channel.history(limit=100):
            if int(getattr(message.author, "id", 0) or 0) != bot_id:
                continue
            if str(getattr(message, "nonce", "") or "") != nonce:
                continue
            if str(getattr(message, "content", "")) != content:
                continue
            return str(message.id)
        return None

    # ---------------------------------------------------------------- internals

    async def _resolve_channel(self, guild_id: int, channel_id: int):
        guild = self._bot.get_guild(int(guild_id))
        if guild is None:
            return None, None
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None, guild
        if not isinstance(channel, discord.TextChannel):
            return None, guild
        return channel, guild

    def _permissions(self, channel: discord.TextChannel, guild: discord.Guild):
        me = guild.me or (guild.get_member(self._bot.user.id) if self._bot.user else None)
        if me is None:
            return None
        return channel.permissions_for(me)


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    status = getattr(exc, "status", None)
    if status:
        return f"http_{status}"
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name)[:64]
