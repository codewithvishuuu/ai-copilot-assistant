"""Mission commands, live progress and Mission Control.

Missions run over the explicit read-only skill registry. Every command rebuilds
the actor and skill scope from Discord's current member state, and every mission
read is guild-scoped by the repository. Cancellation is the only mutation.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from ..missions.models import MissionMode, MissionStatus, MissionView
from ..ui import checks
from ..ui.missions import mission_control_embed, mission_embed, mission_list_embed
from .base import CopilotCog

log = logging.getLogger(__name__)

_TERMINAL = {MissionStatus.completed, MissionStatus.failed, MissionStatus.cancelled}
_MODES = [
    app_commands.Choice(name="Community analysis", value="community"),
    app_commands.Choice(name="Web research", value="research"),
    app_commands.Choice(name="Draft preparation", value="draft_prep"),
]


class MissionCancelButton(discord.ui.Button):
    def __init__(self, mission_id: int) -> None:
        super().__init__(
            label="Cancel mission",
            style=discord.ButtonStyle.danger,
            custom_id=f"mission:cancel:{mission_id}",
            row=0,
        )
        self.mission_id = mission_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, MissionProgressView):
            await view.cog.cancel_from_button(interaction, self.mission_id, view)


class MissionProgressView(discord.ui.View):
    """Short-lived progress buttons; slash commands remain the durable path."""

    def __init__(self, cog: MissionCog, mission_id: int) -> None:
        super().__init__(timeout=1800)
        self.cog = cog
        self.mission_id = mission_id
        self.add_item(MissionCancelButton(mission_id))


class MissionCog(CopilotCog):
    @property
    def missions(self):
        return self.bot.mission_service

    async def _gate(self, interaction: discord.Interaction):
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return self.skill_context(interaction)

    async def _provider_status(self) -> dict[str, str]:
        providers: dict[str, str] = {}
        try:
            health = await self.provider.health()
            providers["Local model"] = (
                f"reachable ({self.settings.ollama_model})"
                if health.reachable
                else "unavailable — start Ollama on the host"
            )
        except Exception:  # noqa: BLE001 - diagnostics must not fail the view
            providers["Local model"] = "unknown — health check failed"
        reason = self.settings.research_unavailable_reason
        providers["Web research"] = "ready" if reason is None else f"unavailable — {reason}"
        providers["Content publishing"] = (
            "enabled"
            if self.settings.content_publishing_enabled
            else "disabled (CONTENT_PUBLISHING_ENABLED=false)"
        )
        return providers

    async def cancel_from_button(
        self,
        interaction: discord.Interaction,
        mission_id: int,
        progress: MissionProgressView,
    ) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.missions.cancel(context, mission_id)
            progress.stop()
            await interaction.edit_original_response(embed=mission_embed(view), view=None)
        except Exception as exc:  # noqa: BLE001 - button callbacks need a safe reply
            await self.handle_error(interaction, exc, action="cancelling the mission")

    # ------------------------------------------------------------------ commands

    @app_commands.command(
        name="mission",
        description="Moderator: run a bounded, read-only multi-step mission with live progress",
    )
    @app_commands.choices(mode=_MODES)
    @app_commands.describe(
        goal="What the mission should find out (evidence-backed, read-only)",
        mode="Which bounded plan to run (default: community analysis)",
    )
    async def mission(
        self,
        interaction: discord.Interaction,
        goal: app_commands.Range[str, 3, 1000],
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        selected = MissionMode(mode.value) if mode is not None else MissionMode.community
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.missions.create_mission(
                context,
                goal=goal,
                mode=selected,
                idempotency_key=f"slash:{interaction.id}",
            )
            progress = MissionProgressView(self, view.mission_id)
            state: dict[str, discord.Message | None] = {"message": None}
            lock = asyncio.Lock()

            async def push(latest: MissionView) -> None:
                async with lock:
                    message = state["message"]
                    if message is None:
                        return
                    try:
                        if latest.status in _TERMINAL:
                            progress.stop()
                            await message.edit(embed=mission_embed(latest), view=None)
                        else:
                            await message.edit(embed=mission_embed(latest), view=progress)
                    except discord.HTTPException:
                        log.debug("Mission progress edit skipped (mission=%s)", latest.mission_id)

            try:
                state["message"] = await interaction.followup.send(
                    embed=mission_embed(view),
                    view=progress,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning("Could not deliver mission progress message (mission=%s)", view.mission_id)

            await self.missions.start(context, view.mission_id, on_update=push)
        except Exception as exc:  # noqa: BLE001 - command boundary must stay safe
            await self.handle_error(interaction, exc, action="starting the mission")

    @app_commands.command(name="missions", description="Moderator: list recent missions in this server")
    async def missions_list(self, interaction: discord.Interaction) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            views = await self.missions.list(guild_id=str(context.guild_id), limit=15)
            await self.reply(interaction, embed=mission_list_embed(views))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="listing missions")

    @app_commands.command(
        name="mission-view",
        description="Moderator: show one mission's steps, trace and evidence",
    )
    async def mission_view(self, interaction: discord.Interaction, mission_id: int) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            view = await self.missions.get(guild_id=str(context.guild_id), mission_id=mission_id)
            await self.reply(interaction, embed=mission_embed(view))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading the mission")

    @app_commands.command(
        name="mission-cancel",
        description="Moderator: request cancellation of a planned or running mission",
    )
    async def mission_cancel(self, interaction: discord.Interaction, mission_id: int) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.missions.cancel(context, mission_id)
            await self.reply(interaction, embed=mission_embed(view))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="cancelling the mission")

    @app_commands.command(
        name="mission-control",
        description="Moderator: mission status, provider availability and recent activity",
    )
    async def mission_control(self, interaction: discord.Interaction) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            control = await self.missions.control_view(
                guild_id=str(context.guild_id), providers=await self._provider_status()
            )
            await self.reply(interaction, embed=mission_control_embed(control))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading Mission Control")
