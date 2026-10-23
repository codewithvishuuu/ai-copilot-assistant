"""Content planning commands: calendar, event plans and reply drafts.

These commands store plans and drafts only. They never create Discord events,
submit approvals or post replies. A calendar entry can optionally be linked to
a durable scheduled job by the scheduler commands; linked jobs run read-only
analysis or create approval-gated drafts, never public posts.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core.content_planning import CalendarKind
from ..errors import NotAllowedError
from ..ui import checks
from ..ui.missions import (
    calendar_embed,
    event_plan_embed,
    event_plan_list_embed,
    reply_draft_embed,
    reply_list_embed,
)
from .base import CopilotCog
from .content import moderator_actor

log = logging.getLogger(__name__)

_KINDS = [
    app_commands.Choice(name="Announcement", value="announcement"),
    app_commands.Choice(name="Event", value="event"),
    app_commands.Choice(name="Post", value="post"),
    app_commands.Choice(name="Reminder", value="reminder"),
]


class PlanningCog(CopilotCog):
    @property
    def planning(self):
        return self.bot.content_planning

    @property
    def scheduler(self):
        return self.bot.scheduler

    async def _calendar_jobs(self, guild_id: str, entries) -> dict[int, object]:
        """Map entry id to its linked scheduled job for honest calendar rendering."""
        job_ids = {entry.scheduled_job_id for entry in entries if entry.scheduled_job_id}
        if not job_ids:
            return {}
        jobs = await self.scheduler.list_jobs(guild_id=guild_id, limit=200)
        by_id = {job.job_id: job for job in jobs if job.job_id in job_ids}
        return {
            entry.entry_id: by_id[entry.scheduled_job_id]
            for entry in entries
            if entry.scheduled_job_id in by_id
        }

    async def _actor(self, interaction: discord.Interaction):
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return moderator_actor(interaction, self.settings)

    def _target_channel_id(self, interaction: discord.Interaction, channel: discord.TextChannel | None) -> str | None:
        """Validate an optional target channel before storing it."""
        if channel is None:
            return None
        if channel.guild is None or channel.guild.id != interaction.guild_id:
            raise NotAllowedError()
        if not checks.channel_allowed(
            self.settings.channel_allowlist, channel.id, channel.parent_id
        ):
            raise NotAllowedError()
        return str(channel.id)

    # ----------------------------------------------------------------- calendar

    @app_commands.command(
        name="calendar",
        description="Moderator: show the content calendar and any linked scheduled job",
    )
    @app_commands.describe(include_cancelled="Also show cancelled entries")
    async def calendar(
        self, interaction: discord.Interaction, include_cancelled: bool = False
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            entries = await self.planning.list_calendar(
                actor, include_cancelled=include_cancelled
            )
            jobs = await self._calendar_jobs(str(actor.guild_id), entries)
            await self.reply(
                interaction,
                embed=calendar_embed(
                    entries, include_cancelled=include_cancelled, jobs=jobs
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading the content calendar")

    @app_commands.command(
        name="calendar-add",
        description="Moderator: add one planned calendar entry (no scheduled job is created)",
    )
    @app_commands.choices(kind=_KINDS)
    @app_commands.describe(
        title="Short plan title",
        kind="What kind of plan this is",
        starts_at="ISO start date/time, e.g. 2026-11-05T18:00:00+00:00",
        ends_at="Optional ISO end date/time",
        channel="Optional target channel",
        notes="Optional internal notes",
        draft_id="Optional Content Studio draft id to link",
        event_plan_id="Optional event plan id to link",
    )
    async def calendar_add(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 3, 200],
        kind: app_commands.Choice[str],
        starts_at: app_commands.Range[str, 10, 40],
        ends_at: app_commands.Range[str, 10, 40] | None = None,
        channel: discord.TextChannel | None = None,
        notes: app_commands.Range[str, 0, 1000] | None = None,
        draft_id: app_commands.Range[int, 1, 2_000_000_000] | None = None,
        event_plan_id: app_commands.Range[int, 1, 2_000_000_000] | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            entry = await self.planning.create_calendar_entry(
                actor,
                title=title,
                kind=CalendarKind(kind.value),
                starts_at=starts_at,
                ends_at=ends_at,
                target_channel_id=self._target_channel_id(interaction, channel),
                notes=notes or "",
                draft_id=draft_id,
                event_plan_id=event_plan_id,
            )
            await self.reply(interaction, embed=calendar_embed([entry], include_cancelled=True))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="adding the calendar entry")

    @app_commands.command(
        name="calendar-cancel",
        description="Moderator: cancel a calendar entry and its linked scheduled job",
    )
    async def calendar_cancel(self, interaction: discord.Interaction, entry_id: int) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            entry = await self.planning.cancel_calendar_entry(actor, entry_id)
            await self.scheduler.cancel_for_calendar_entry(
                guild_id=str(actor.guild_id),
                entry_id=entry_id,
                actor_id=actor.actor_id,
            )
            jobs = await self._calendar_jobs(str(actor.guild_id), [entry])
            await self.reply(
                interaction,
                embed=calendar_embed([entry], include_cancelled=True, jobs=jobs),
            )
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="cancelling the calendar entry")

    # --------------------------------------------------------------- event plans

    @app_commands.command(
        name="event-plan",
        description="Moderator: create a deterministic event plan template (nothing is created in Discord)",
    )
    @app_commands.describe(
        title="Event title",
        brief="Goal and rough format of the event",
        scheduled_for="Optional ISO start date/time",
        channel="Optional proposed channel",
    )
    async def event_plan(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 3, 200],
        brief: app_commands.Range[str, 5, 2000],
        scheduled_for: app_commands.Range[str, 10, 40] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            plan = await self.planning.create_event_plan(
                actor,
                title=title,
                brief=brief,
                scheduled_for=scheduled_for,
                target_channel_id=self._target_channel_id(interaction, channel),
            )
            await self.reply(interaction, embed=event_plan_embed(plan))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="creating the event plan")

    @app_commands.command(name="event-plans", description="Moderator: list event plans")
    async def event_plans(self, interaction: discord.Interaction) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            plans = await self.planning.list_event_plans(actor)
            await self.reply(interaction, embed=event_plan_list_embed(plans))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="listing event plans")

    @app_commands.command(name="event-plan-view", description="Moderator: view one event plan")
    async def event_plan_view(self, interaction: discord.Interaction, plan_id: int) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            plan = await self.planning.get_event_plan(actor, plan_id)
            await self.reply(interaction, embed=event_plan_embed(plan))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading the event plan")

    @app_commands.command(name="event-plan-archive", description="Moderator: archive one event plan")
    async def event_plan_archive(self, interaction: discord.Interaction, plan_id: int) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            plan = await self.planning.archive_event_plan(actor, plan_id)
            await self.reply(interaction, embed=event_plan_embed(plan))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="archiving the event plan")

    # -------------------------------------------------------------- reply drafts

    @app_commands.command(
        name="reply-draft",
        description="Moderator: draft (never send) a contextual reply from a pasted excerpt",
    )
    @app_commands.describe(
        source="The message or context to reply to (pasted by you)",
        purpose="What the reply should accomplish",
        tone="Requested tone",
    )
    async def reply_draft(
        self,
        interaction: discord.Interaction,
        source: app_commands.Range[str, 3, 3000],
        purpose: app_commands.Range[str, 0, 300] | None = None,
        tone: app_commands.Range[str, 0, 100] | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            reply = await self.planning.generate_reply_draft(
                actor,
                self.provider,
                source_excerpt=source,
                purpose=purpose or "",
                tone=tone or "",
                channel_id=str(interaction.channel_id or "unknown"),
                source_message_id="manual",
            )
            await self.reply(interaction, embed=reply_draft_embed(reply))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="drafting the reply")

    @app_commands.command(name="reply-drafts", description="Moderator: list open reply drafts")
    async def reply_drafts(self, interaction: discord.Interaction) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            replies = await self.planning.list_reply_drafts(actor)
            await self.reply(interaction, embed=reply_list_embed(replies))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="listing reply drafts")

    @app_commands.command(name="reply-view", description="Moderator: view one reply draft")
    async def reply_view(self, interaction: discord.Interaction, reply_id: int) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            reply = await self.planning.get_reply_draft(actor, reply_id)
            await self.reply(interaction, embed=reply_draft_embed(reply))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="loading the reply draft")

    @app_commands.command(name="reply-dismiss", description="Moderator: dismiss one reply draft")
    async def reply_dismiss(self, interaction: discord.Interaction, reply_id: int) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            reply = await self.planning.dismiss_reply_draft(actor, reply_id)
            await self.reply(interaction, embed=reply_draft_embed(reply))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="dismissing the reply draft")
