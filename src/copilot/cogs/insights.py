"""`/insights` — moderator-facing aggregates with a minimum-data gate."""

from __future__ import annotations

import discord
from discord import app_commands

from ..core.insights_engine import compute_insights
from ..ui import checks, embeds
from .base import CopilotCog


class InsightsCog(CopilotCog):
    @app_commands.command(
        name="insights",
        description="Moderator: recurring issues and open questions seen recently (minimum-data gated)",
    )
    async def insights(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        result = await compute_insights(
            self.repo,
            window_days=self.settings.insights_window_days,
            minimum_threads=self.settings.insights_min_threads,
        )
        await self.reply(interaction, embed=embeds.build_insights_embed(result))
