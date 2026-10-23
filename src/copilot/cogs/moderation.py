"""Moderator commands: status corrections and the solution approval workflow."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core import solutions
from ..db import retention
from ..ui import checks
from .base import CopilotCog

log = logging.getLogger(__name__)

_STATUS_CHOICES = [
    app_commands.Choice(name="Unresolved", value="unresolved"),
    app_commands.Choice(name="In progress", value="in_progress"),
    app_commands.Choice(name="Waiting for user", value="waiting_for_user"),
    app_commands.Choice(name="Resolved", value="resolved"),
    app_commands.Choice(name="Unclear", value="unclear"),
]


class ModerationCog(CopilotCog):
    # ------------------------------------------------------------ corrections

    @app_commands.command(
        name="correct-status",
        description="Moderator: correct the AI's detected status for a thread",
    )
    @app_commands.choices(status=_STATUS_CHOICES)
    @app_commands.describe(thread="The thread whose status you are correcting", note="Why (optional)")
    async def correct_status(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread,
        status: app_commands.Choice[str],
        note: str | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        if not await checks.ensure_target_thread(interaction, thread, self.settings):
            return
        # Ack before the first state change - same class as reject_solution: a stall
        # between here and the reply can miss Discord's 3-second initial-response
        # window while the correction sticks. reply() then uses the follow-up window.
        await interaction.response.defer(ephemeral=True)
        thread_row_id = await self._ensure_thread_row(thread)
        await self.repo.add_correction(
            thread_id=thread_row_id,
            status=status.value,
            actor_id=str(interaction.user.id),
            note=note,
        )
        await self.repo.audit(
            actor_id=str(interaction.user.id),
            action="status.corrected",
            target_type="thread",
            target_id=str(thread.id),
            guild_id=str(interaction.guild_id),
            meta={"guild_id": str(interaction.guild_id), "status": status.value},
        )
        await self.reply(
            interaction,
            content=f"Correction recorded: **{status.name}** for <#{thread.id}>.",
        )

    # -------------------------------------------------------------- solutions

    @app_commands.command(
        name="resolve",
        description="Moderator: propose the solution for a thread (needs approval to become official)",
    )
    @app_commands.describe(
        thread="The thread being resolved",
        solution="The solution text (10–1000 characters)",
        note="Extra context for the knowledge base (optional)",
    )
    async def resolve(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread,
        solution: app_commands.Range[str, 10, 1000],
        note: str | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        if not await checks.ensure_target_thread(interaction, thread, self.settings):
            return
        # Ack before the first state change (same reason as reject_solution).
        await interaction.response.defer(ephemeral=True)
        thread_row_id = await self._ensure_thread_row(thread)
        body = solution
        if note:
            body += f"\n\nModerator note: {note}"
        source_url = getattr(thread, "jump_url", None)
        solution_id = await solutions.propose_solution(
            self.repo,
            thread_row_id=thread_row_id,
            body=body,
            source_url=str(source_url) if source_url else None,
            actor_id=str(interaction.user.id),
        )
        await self.reply(
            interaction,
            content=(
                f"Solution proposed with id `{solution_id}`.\n"
                f"Approve it with `/approve-solution solution_id:{solution_id}` to add it to the "
                "knowledge base, or reject it with `/reject-solution`."
            ),
        )

    @app_commands.command(
        name="approve-solution",
        description="Moderator: approve a proposed solution and add it to the knowledge base",
    )
    async def approve_solution(
        self, interaction: discord.Interaction, solution_id: app_commands.Range[int, 1]
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        # Ack before the state change (same reason as reject_solution).
        await interaction.response.defer(ephemeral=True)
        ok, reason = await solutions.approve_solution(
            self.repo,
            solution_id=solution_id,
            guild_id=str(interaction.guild_id),
            actor_id=str(interaction.user.id),
        )
        if ok:
            message = f"Solution `{solution_id}` approved and added to the knowledge base."
        elif reason == "not_found":
            message = f"No proposed solution with id `{solution_id}`."
        else:
            message = f"Solution `{solution_id}` was already {reason.removeprefix('already_')}."
        await self.reply(interaction, content=message)

    @app_commands.command(
        name="reject-solution",
        description="Moderator: reject a proposed solution",
    )
    async def reject_solution(
        self,
        interaction: discord.Interaction,
        solution_id: app_commands.Range[int, 1],
        note: str | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        # Ack before the state change: live 2026-10-19, this fast handler still lost
        # its reply to an environmental stall that pushed the POST past Discord's
        # 3-second initial-response window (404/10062 "Unknown interaction") while
        # the rejection stuck. Deferring keeps the confirmation inside the
        # 15-minute follow-up window (reply() already routes to followup.send).
        await interaction.response.defer(ephemeral=True)
        ok, reason = await solutions.reject_solution(
            self.repo,
            solution_id=solution_id,
            guild_id=str(interaction.guild_id),
            actor_id=str(interaction.user.id),
            note=note,
        )
        if ok:
            message = f"Solution `{solution_id}` rejected. It stays stored but is not searchable."
        elif reason == "not_found":
            message = f"No proposed solution with id `{solution_id}`."
        else:
            message = f"Solution `{solution_id}` was already {reason.removeprefix('already_')}."
        await self.reply(interaction, content=message)

    # ----------------------------------------------------------------- helpers

    async def _ensure_thread_row(self, thread: discord.Thread) -> int:
        expiry = retention.expiry_from_now(self.settings.retention_days)
        parent_id = getattr(thread, "parent_id", None)
        guild = getattr(thread, "guild", None)
        return await self.repo.upsert_thread(
            discord_thread_id=str(thread.id),
            guild_id=str(guild.id) if guild is not None else "",
            parent_channel_id=str(parent_id) if parent_id is not None else None,
            name=str(getattr(thread, "name", "") or ""),
            retention_expires_at=expiry,
        )
