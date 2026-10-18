"""`/debug` — developer-only error/stack-trace explainer (nothing is ever executed)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..errors import CopilotError, NotDevUserError, new_error_code
from ..ui import checks, embeds
from .base import CopilotCog

log = logging.getLogger(__name__)


class DebugModal(discord.ui.Modal, title="Explain an error or stack trace"):
    error_text: discord.ui.TextInput = discord.ui.TextInput(
        label="Error text / stack trace",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True,
        placeholder="Paste the error message or traceback here…",
    )

    def __init__(self, cog: DebugCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.run_debug(interaction, str(self.error_text.value))


class DebugCog(CopilotCog):
    @app_commands.command(
        name="debug",
        description="Explain an error or stack trace (developer-only; nothing is executed)",
    )
    async def debug(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_dev_user(interaction, self.settings):
            return
        await interaction.response.send_modal(DebugModal(self))

    async def run_debug(self, interaction: discord.Interaction, error_text: str) -> None:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if not checks.is_dev_user(self.settings.dev_user_ids, user_id):
            await self.reply(interaction, content=str(NotDevUserError()))
            return
        if not error_text.strip():
            await self.reply(interaction, content="No error text was provided.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            job = await self.queue.submit(
                user_id=user_id,
                guild_id=interaction.guild_id,
                run=lambda: self.analyzer.explain_error(error_text),
                timeout_s=min(self.settings.job_timeout_s, 240.0),
            )
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return

        try:
            result = await job.future
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        except Exception:  # noqa: BLE001 - never crash a command
            code = new_error_code()
            log.exception("Debug job %s crashed (%s)", job.id, code)
            await self.reply(interaction, content=f"Unexpected error while analyzing. `{code}`")
            return

        await self.reply(interaction, embed=embeds.build_debug_embed(result))
