"""Shared cog base: subsystem access + mention-safe replies."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

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

    # Replies --------------------------------------------------------------

    async def reply(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        ephemeral: bool = True,
    ) -> None:
        """Send content/embed(s) as the interaction response or follow-up.

        Mentions are always disabled, so untrusted text can never ping anyone.
        """
        if embed is not None and embeds is not None:
            raise ValueError("pass embed or embeds, not both")
        kwargs = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content=content, embed=embed, embeds=embeds, **kwargs)
            else:
                await interaction.response.send_message(
                    content=content, embed=embed, embeds=embeds, **kwargs
                )
        except discord.HTTPException:
            log.warning("Could not deliver interaction reply (HTTPException)")
