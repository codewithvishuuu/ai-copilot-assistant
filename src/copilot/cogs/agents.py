"""Main Host Agent commands: bounded plans, live progress, cancellation, control.

Every command rebuilds the actor scope from Discord's current member state, and
every read is guild-scoped by the repository. Cancellation is the only mutation;
the host itself delegates only to registered read-only skills.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from ..agents.host import AgentRunStatus, AgentRunView, HostMode
from ..ui import checks
from ..ui.agents import agent_control_embed, agent_list_embed, agent_run_embed
from .base import CopilotCog

log = logging.getLogger(__name__)

_TERMINAL = {
    AgentRunStatus.completed,
    AgentRunStatus.partial,
    AgentRunStatus.failed,
    AgentRunStatus.cancelled,
}
_MODES = [
    app_commands.Choice(name="Community analysis", value="community"),
    app_commands.Choice(name="Web research", value="research"),
    app_commands.Choice(name="Draft preparation", value="draft_prep"),
    app_commands.Choice(name="Moderation review", value="moderation"),
    app_commands.Choice(name="Troubleshooting triage", value="triage"),
]


class AgentCancelButton(discord.ui.Button):
    def __init__(self, run_id: int) -> None:
        super().__init__(
            label="Cancel run",
            style=discord.ButtonStyle.danger,
            custom_id=f"agent:cancel:{run_id}",
            row=0,
        )
        self.run_id = run_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AgentRunProgressView):
            await view.cog.cancel_from_button(interaction, self.run_id, view)


class AgentRunProgressView(discord.ui.View):
    """Short-lived progress buttons; slash commands remain the durable path."""

    def __init__(self, cog: AgentCog, run_id: int) -> None:
        super().__init__(timeout=1800)
        self.cog = cog
        self.run_id = run_id
        self.add_item(AgentCancelButton(run_id))


class AgentCog(CopilotCog):
    @property
    def agents(self):
        return self.bot.agent_service

    async def _gate(self, interaction: discord.Interaction):
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return self.skill_context(interaction)

    async def _provider_status(self) -> dict[str, str]:
        providers: dict[str, str] = {}
        providers["Agent host"] = (
            "enabled" if self.settings.agent_enabled else "disabled (AGENT_ENABLED=false)"
        )
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
        run_id: int,
        progress: AgentRunProgressView,
    ) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.agents.cancel(context, run_id)
            progress.stop()
            await interaction.edit_original_response(embed=agent_run_embed(view), view=None)
        except Exception as exc:  # noqa: BLE001 - button callbacks need a safe reply
            await self.handle_error(interaction, exc, action="cancelling the agent run")

    # ------------------------------------------------------------------ commands

    @app_commands.command(
        name="agent",
        description="Moderator: run a bounded plan across specialist read-only roles",
    )
    @app_commands.choices(mode=_MODES)
    @app_commands.describe(
        goal="What the host should accomplish (evidence-backed, read-only)",
        mode="Which bounded plan to run (default: inferred from the goal)",
    )
    async def agent(
        self,
        interaction: discord.Interaction,
        goal: app_commands.Range[str, 3, 500],
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        selected = HostMode(mode.value) if mode is not None else None
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.agents.create_run(
                context,
                goal=goal,
                mode=selected,
                idempotency_key=f"slash:{interaction.id}",
            )
            progress = AgentRunProgressView(self, view.run_id)
            if view.status in _TERMINAL:
                progress.stop()
            state: dict[str, discord.Message | None] = {"message": None}
            lock = asyncio.Lock()

            async def push(latest: AgentRunView) -> None:
                async with lock:
                    message = state["message"]
                    if message is None:
                        return
                    try:
                        if latest.status in _TERMINAL:
                            progress.stop()
                            await message.edit(embed=agent_run_embed(latest), view=None)
                        else:
                            await message.edit(embed=agent_run_embed(latest), view=progress)
                    except discord.HTTPException:
                        log.debug("Agent progress edit skipped (run=%s)", latest.run_id)

            try:
                state["message"] = await interaction.followup.send(
                    embed=agent_run_embed(view),
                    view=None if view.status in _TERMINAL else progress,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning("Could not deliver agent progress message (run=%s)", view.run_id)

            await self.agents.start(context, view.run_id, on_update=push)
        except Exception as exc:  # noqa: BLE001 - command boundary must stay safe
            await self.handle_error(interaction, exc, action="starting the agent run")

    @app_commands.command(name="agent-runs", description="Moderator: list recent agent runs in this server")
    async def agent_runs(self, interaction: discord.Interaction) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            views = await self.agents.list(guild_id=str(context.guild_id), limit=15)
            await self.reply(interaction, embed=agent_list_embed(views))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="listing agent runs")

    @app_commands.command(
        name="agent-view",
        description="Moderator: show one agent run's steps, dependencies and evidence",
    )
    async def agent_view(self, interaction: discord.Interaction, run_id: int) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            view = await self.agents.get(guild_id=str(context.guild_id), run_id=run_id)
            await self.reply(interaction, embed=agent_run_embed(view))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading the agent run")

    @app_commands.command(
        name="agent-cancel",
        description="Moderator: request cancellation of a planned or running agent run",
    )
    async def agent_cancel(self, interaction: discord.Interaction, run_id: int) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.agents.cancel(context, run_id)
            await self.reply(interaction, embed=agent_run_embed(view))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="cancelling the agent run")

    @app_commands.command(
        name="agent-control",
        description="Moderator: agent status, role availability, providers and recent runs",
    )
    async def agent_control(self, interaction: discord.Interaction) -> None:
        context = await self._gate(interaction)
        if context is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            control = await self.agents.control_view(
                guild_id=str(context.guild_id), providers=await self._provider_status()
            )
            await self.reply(interaction, embed=agent_control_embed(control))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading Agent Control")
