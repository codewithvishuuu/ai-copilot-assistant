"""Web research commands and the research-to-draft provenance bridge.

Search runs only through the configured provider. When it is unavailable the
result says so explicitly and persists an ``unavailable`` report; no citations
are invented. The draft bridge transfers a completed report into Content Studio
as a draft with ``source_kind=research`` and never submits or publishes it.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..research.service import ResearchStatus, create_draft_from_research
from ..ui import checks
from ..ui.content import DiscordContentSender
from ..ui.missions import research_embed
from .base import CopilotCog
from .content import moderator_actor

log = logging.getLogger(__name__)


class ResearchDraftButton(discord.ui.Button):
    def __init__(self, report_id: int) -> None:
        super().__init__(
            label="Create content draft",
            style=discord.ButtonStyle.primary,
            custom_id=f"research:draft:{report_id}",
            row=0,
        )
        self.report_id = report_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ResearchResultView):
            await view.cog.create_draft_from_button(interaction, self.report_id, view)


class ResearchResultView(discord.ui.View):
    """One-shot draft button; creating a draft is not publication."""

    def __init__(self, cog: ResearchCog, report_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.add_item(ResearchDraftButton(report_id))


class ResearchCog(CopilotCog):
    @property
    def research(self):
        return self.bot.research_service

    async def _actor(self, interaction: discord.Interaction):
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return moderator_actor(interaction, self.settings)

    async def _create_draft(
        self, interaction: discord.Interaction, report_id: int, channel: discord.TextChannel
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            target = await DiscordContentSender(self.bot).resolve_target(
                int(interaction.guild_id), int(channel.id)
            )
            draft = await create_draft_from_research(
                self.repo,
                self.bot.content_studio,
                actor,
                report_id=report_id,
                target=target,
                settings=self.settings,
            )
            await self.reply(
                interaction,
                content=(
                    f"Created content draft `{draft.draft_id}` (version {draft.version}) from "
                    f"research report `{report_id}` with provenance "
                    f"`{draft.source_kind}:{draft.source_ref}`. It was not submitted, approved, "
                    "scheduled or published. Review it with `/content-view`."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - command boundary must stay safe
            await self.handle_error(interaction, exc, action="creating the research draft")

    async def create_draft_from_button(
        self,
        interaction: discord.Interaction,
        report_id: int,
        view: ResearchResultView,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.reply(interaction, content="Use this button from a normal text channel.")
            return
        view.stop()
        await self._create_draft(interaction, report_id, channel)

    # ------------------------------------------------------------------ commands

    @app_commands.command(
        name="research",
        description="Moderator: search the configured web provider and cite the sources",
    )
    @app_commands.describe(query="What to research (bounded to one search)")
    async def research_command(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 3, 500],
    ) -> None:
        if await self._actor(interaction) is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            outcome = await self.research.research(
                guild_id=str(interaction.guild_id),
                query=query,
                llm=self.provider,
            )
            view = None
            if outcome.status is ResearchStatus.completed and outcome.report_id is not None:
                view = ResearchResultView(self, outcome.report_id)
            await self.reply(interaction, embed=research_embed(outcome), view=view)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="running web research")

    @app_commands.command(
        name="research-draft",
        description="Moderator: turn a completed research report into a Content Studio draft",
    )
    @app_commands.describe(
        report_id="Report id shown by /research",
        channel="Intended public channel (defaults to this channel)",
    )
    async def research_draft(
        self,
        interaction: discord.Interaction,
        report_id: int,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await self.reply(interaction, content="Choose a normal text channel as the target.")
            return
        await self._create_draft(interaction, report_id, target_channel)
