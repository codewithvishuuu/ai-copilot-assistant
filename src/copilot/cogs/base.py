"""Shared cog base: subsystem access + mention-safe replies."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..agents.contract import SkillContext, SkillPermission
from ..errors import CopilotError, NotAllowedError, new_error_code
from ..util.logging import redact

if TYPE_CHECKING:
    from ..bot import CommunityCopilot

log = logging.getLogger(__name__)


class CopilotCog(commands.Cog):
    def __init__(self, bot: CommunityCopilot) -> None:
        self.bot = bot

    # Convenience accessors ------------------------------------------------

    @property
    def settings(self):
        return self.bot.settings

    @property
    def provider(self):
        return self.bot.provider

    @property
    def queue(self):
        return self.bot.queue

    @property
    def db(self):
        return self.bot.db

    @property
    def repo(self):
        return self.bot.repo

    @property
    def analyzer(self):
        return self.bot.analyzer

    # Trusted caller scope ------------------------------------------------

    def skill_context(self, interaction: discord.Interaction) -> SkillContext:
        """Build the trusted skill scope from the current interaction.

        Only call this after the moderator gate succeeded; the context carries
        exactly the permissions the Discord adapter verified, never model input.
        """
        guild_id = getattr(interaction, "guild_id", None)
        channel = getattr(interaction, "channel", None)
        channel_id = getattr(channel, "id", None) or getattr(interaction, "channel_id", None)
        user = getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        if guild_id is None or channel_id is None or user_id is None:
            raise NotAllowedError()
        return SkillContext(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            parent_id=(
                None if getattr(channel, "parent_id", None) is None else int(channel.parent_id)
            ),
            user_id=int(user_id),
            permissions=frozenset(
                {SkillPermission.guild_member, SkillPermission.moderator}
            ),
        )

    # Replies --------------------------------------------------------------

    async def handle_error(
        self,
        interaction: discord.Interaction,
        exc: BaseException,
        *,
        action: str,
    ) -> None:
        """Deliver one bounded error message; never leak exception text to users."""
        code = new_error_code()
        if isinstance(exc, CopilotError):
            message = exc.user_message()
            log.info("%s error %s: %s", action, code, exc)
            if exc.detail:
                log.debug("%s error %s detail: %s", action, code, exc.detail)
        else:
            message = f"Unexpected error while {action}."
            log.exception("Unhandled %s error %s: %s", action, code, exc)
        try:
            await self.reply(interaction, content=f"{message}\n`{code}`")
        except Exception:  # pragma: no cover - interaction may be gone
            log.warning("Could not deliver %s error %s", action, code)

    async def reply(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ) -> None:
        """Send content/embed(s) as the interaction response or follow-up.

        Mentions are always disabled, so untrusted text can never ping anyone.
        """
        if embed is not None and embeds is not None:
            raise ValueError("pass embed or embeds, not both")
        # discord.py uses a MISSING sentinel: an explicit `embed=None` counts as
        # "provided" and raises TypeError("Cannot mix embed and embeds keyword
        # arguments.") when `embeds` is also passed (live /status crash CC-8480),
        # so forward only the form the caller actually used.
        kwargs: dict = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
        if embeds is not None:
            kwargs["embeds"] = embeds
        elif embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content=content, **kwargs)
            else:
                await interaction.response.send_message(content=content, **kwargs)
        except discord.HTTPException as exc:
            # Never swallow the reason: a bare "(HTTPException)" made live reply
            # failures (e.g. /cancel "did not respond") impossible to diagnose.
            log.warning("Could not deliver interaction reply (HTTPException): %s", redact(str(exc))[:200])
