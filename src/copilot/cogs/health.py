"""`/status` — truthful health of every subsystem."""

from __future__ import annotations

import discord
from discord import app_commands

from .. import __version__
from ..core.health import gather_health
from ..ui import checks, embeds
from .base import CopilotCog


class HealthCog(CopilotCog):
    @app_commands.command(
        name="status",
        description="Community Copilot health: Discord, local AI model, database, and queue",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        await interaction.response.defer(ephemeral=True)
        report = await gather_health(
            provider=self.provider,
            db=self.db,
            queue=self.queue,
            cooldown_for=interaction.user.id,
            bot_latency_s=self.bot.latency,
            guild_count=len(self.bot.guilds),
            version=__version__,
        )
        allowlisted = checks.guild_allowed(self.settings.guild_allowlist, interaction.guild_id)
        await self.reply(
            interaction,
            embeds=embeds.build_status_embeds(
                report,
                allowlisted=allowlisted,
                cooldown_s=self.settings.per_user_cooldown_s,
            ),
        )
