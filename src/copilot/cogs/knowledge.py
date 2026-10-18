"""Knowledge base commands: `/kb-search` and the privacy purge `/forget`."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..db import retention
from ..ui import checks, embeds
from .base import CopilotCog

log = logging.getLogger(__name__)


class KnowledgeCog(CopilotCog):
    @app_commands.command(
        name="kb-search",
        description="Search approved solutions and past thread summaries (local knowledge base)",
    )
    async def kb_search(
        self, interaction: discord.Interaction, query: app_commands.Range[str, 3, 200]
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        rows = await self.repo.search_kb(query, limit=5)
        if not rows:
            await self.reply(
                interaction,
                content=(
                    "No matches in the knowledge base yet. Approved solutions and summarized "
                    "threads become searchable here."
                ),
            )
            return
        await self.reply(interaction, embeds=embeds.build_kb_search_embeds(query, rows))

    @app_commands.command(
        name="forget",
        description="Moderator: permanently delete ALL stored data for one thread",
    )
    async def forget(self, interaction: discord.Interaction, thread: discord.Thread) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return

        counts = await retention.purge_thread(self.db, str(thread.id))
        await self.repo.audit(
            actor_id=str(interaction.user.id),
            action="thread.forgotten",
            target_type="thread",
            target_id=str(thread.id),
            meta={"deleted": counts},
        )
        if not counts:
            await self.reply(
                interaction,
                content="Nothing was stored for that thread — there is nothing to delete.",
            )
            return
        summary = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
        await self.reply(
            interaction,
            content=(
                f"Deleted all stored data for <#{thread.id}>: {summary}. "
                "This cannot be undone."
            ),
        )
        log.info("Thread %s purged by moderator %s: %s", thread.id, interaction.user.id, counts)
