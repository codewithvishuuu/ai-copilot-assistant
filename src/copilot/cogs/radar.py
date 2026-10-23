"""Problem Radar: evidence-backed recurring issues, gaps and uncertainty."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core.problem_radar import compute_problem_radar
from ..ui import checks
from ..ui.missions import radar_embed
from .base import CopilotCog

log = logging.getLogger(__name__)


class RadarCog(CopilotCog):
    @app_commands.command(
        name="radar",
        description="Moderator: recurring questions, recurring errors, open threads and knowledge gaps",
    )
    async def radar(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            snapshot = await compute_problem_radar(
                self.repo,
                guild_id=str(interaction.guild_id),
                window_days=self.settings.insights_window_days,
                minimum_threads=self.settings.insights_min_threads,
            )
            await self.reply(interaction, embed=radar_embed(snapshot))
        except Exception as exc:  # noqa: BLE001 - command boundary must stay safe
            await self.handle_error(interaction, exc, action="computing the Problem Radar")
