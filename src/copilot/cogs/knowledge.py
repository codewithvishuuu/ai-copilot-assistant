"""Knowledge base commands: `/kb-search` and the privacy purge `/forget`."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core.knowledge_candidates import (
    accept_proposal,
    discover_from_summary,
    list_candidates,
    review_candidate,
)
from ..core.knowledge_purge import purge_approved_document
from ..db import retention
from ..errors import CopilotError
from ..ui import checks, embeds
from .base import CopilotCog
from .content import moderator_actor

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
        if interaction.guild_id is None:
            await self.reply(interaction, content="Knowledge search only works inside a server.")
            return
        try:
            rows = await self.repo.search_kb(
                query,
                guild_id=str(interaction.guild_id),
                limit=5,
                approved_only=True,
            )
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
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
        name="knowledge-candidates",
        description="Moderator: list unapproved knowledge candidates awaiting review",
    )
    async def knowledge_candidates(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        actor = moderator_actor(interaction, self.settings)
        await interaction.response.defer(ephemeral=True)
        try:
            views = await list_candidates(actor, self.repo, state="proposed", limit=10)
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        if not views:
            await self.reply(interaction, content="No knowledge candidates are awaiting review.")
            return
        lines = []
        for view in views:
            flags = []
            if view.stale:
                flags.append("stale source")
            if view.source_summary_id is not None:
                flags.append(f"summary {view.source_summary_id}")
            lines.append(
                f"`{view.candidate_id}` [{view.source_kind}] {view.thread_name or 'thread'} — "
                f"{view.body[:80]}" + (f" ({', '.join(flags)})" if flags else "")
            )
        await self.reply(
            interaction,
            content=(
                "Unapproved knowledge candidates (not searchable until approved):\n"
                + "\n".join(lines)
                + "\nUse `/knowledge-candidate-approve` or `/knowledge-candidate-reject`."
            ),
        )

    @app_commands.command(
        name="knowledge-candidate-propose",
        description="Moderator: accept a candidate from one of this server's stored summaries",
    )
    async def knowledge_candidate_propose(
        self, interaction: discord.Interaction, summary_id: app_commands.Range[int, 1]
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        actor = moderator_actor(interaction, self.settings)
        await interaction.response.defer(ephemeral=True)
        try:
            proposal = await discover_from_summary(
                self.repo, guild_id=str(interaction.guild_id), summary_id=int(summary_id)
            )
            view = await accept_proposal(actor, self.repo, proposal)
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        await self.reply(
            interaction,
            content=(
                f"Stored knowledge candidate `{view.candidate_id}` as unapproved. "
                "It is not in the knowledge base or chat grounding until a moderator "
                "approves the exact text."
            ),
        )

    @app_commands.command(
        name="knowledge-candidate-approve",
        description="Moderator: approve one knowledge candidate into the searchable knowledge base",
    )
    async def knowledge_candidate_approve(
        self, interaction: discord.Interaction, candidate_id: app_commands.Range[int, 1]
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        actor = moderator_actor(interaction, self.settings)
        await interaction.response.defer(ephemeral=True)
        try:
            view = await review_candidate(
                actor, self.repo, candidate_id=int(candidate_id), decision="approve"
            )
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        await self.reply(
            interaction,
            content=(
                f"Knowledge candidate `{view.candidate_id}` approved and added to this "
                "server's knowledge base. Existing approved answers were not modified."
            ),
        )

    @app_commands.command(
        name="knowledge-candidate-reject",
        description="Moderator: reject one knowledge candidate (stored, not searchable)",
    )
    async def knowledge_candidate_reject(
        self,
        interaction: discord.Interaction,
        candidate_id: app_commands.Range[int, 1],
        note: str | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        actor = moderator_actor(interaction, self.settings)
        await interaction.response.defer(ephemeral=True)
        try:
            view = await review_candidate(
                actor,
                self.repo,
                candidate_id=int(candidate_id),
                decision="reject",
                note=note,
            )
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        await self.reply(
            interaction,
            content=(
                f"Knowledge candidate `{view.candidate_id}` rejected. It stays stored for "
                "audit but is not searchable or used in chat."
            ),
        )

    @app_commands.command(
        name="knowledge-purge",
        description="Moderator: permanently delete one approved knowledge document by id",
    )
    @app_commands.describe(
        document_id="Document id shown by /kb-search as `doc N`",
    )
    async def knowledge_purge(
        self, interaction: discord.Interaction, document_id: app_commands.Range[int, 1]
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        actor = moderator_actor(interaction, self.settings)
        await interaction.response.defer(ephemeral=True)
        try:
            purged = await purge_approved_document(
                actor, self.repo, document_id=int(document_id)
            )
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        await self.reply(
            interaction,
            content=(
                f"Permanently purged approved knowledge document `{purged.document_id}` "
                f"({purged.kind}). It is removed from search results and chat grounding; "
                "the deletion is audited. This cannot be undone."
            ),
        )
        log.info(
            "KB document %s purged by moderator %s (kind=%s)",
            purged.document_id,
            interaction.user.id,
            purged.kind,
        )

    @app_commands.command(
        name="forget",
        description="Moderator: permanently delete a thread's stored content and records",
    )
    async def forget(self, interaction: discord.Interaction, thread: discord.Thread) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        if not await checks.ensure_target_thread(interaction, thread, self.settings):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            # The audit row is written in the same transaction as the deletes: a
            # failed audit must not leave unlogged, irreversible data loss.
            counts = await retention.purge_thread(
                self.db,
                str(thread.id),
                guild_id=str(interaction.guild_id),
                audit_actor_id=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001 - interaction callback must reply safely
            await self.handle_error(interaction, exc, action="forgetting the thread")
            return
        if not any(counts.values()):
            await self.reply(
                interaction,
                content="Nothing was stored for that thread — there is nothing to delete.",
            )
            return
        summary = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
        await self.reply(
            interaction,
            content=(
                f"Deleted stored thread content for <#{thread.id}>: {summary}. "
                "Audit records and internal workflow history (agent runs, drafts, "
                "scheduled jobs) are retained. This cannot be undone."
            ),
        )
        log.info("Thread %s purged by moderator %s: %s", thread.id, interaction.user.id, counts)
