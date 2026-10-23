"""Offline agent evaluation commands (developer-only).

The suite is deterministic and local: contract, permission, guild-isolation,
approval, redaction, image and migration checks against temporary databases. It
never measures model answer quality, and every report says so.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core.evaluation import EvaluationReport
from ..errors import AgentEvalError
from ..ui import checks
from ..ui.missions import evaluation_embed
from .base import CopilotCog

log = logging.getLogger(__name__)


class EvaluationCog(CopilotCog):
    @property
    def evaluations(self):
        return self.bot.evaluation_service

    @app_commands.command(
        name="evaluate",
        description="Developer: run the offline contract, safety, isolation and regression suite",
    )
    async def evaluate(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_dev_user(interaction, self.settings):
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            report = await self.evaluations.run(
                guild_id=str(interaction.guild_id),
                requested_by=str(interaction.user.id),
            )
            await self.reply(interaction, embed=evaluation_embed(report))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="running the offline evaluation")

    @app_commands.command(
        name="evaluation-latest",
        description="Developer: show the last persisted offline evaluation report",
    )
    async def evaluation_latest(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_dev_user(interaction, self.settings):
            return
        try:
            row = await self.repo.latest_agent_eval_run(guild_id=str(interaction.guild_id))
            if row is None:
                await self.reply(
                    interaction,
                    content="No evaluation report has been stored for this server yet. Run `/evaluate`.",
                )
                return
            try:
                report = EvaluationReport.model_validate_json(str(row["report_json"]))
            except ValueError as exc:
                raise AgentEvalError(
                    "The stored evaluation report could not be read safely; run `/evaluate` again."
                ) from exc
            await self.reply(interaction, embed=evaluation_embed(report))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading the evaluation report")
