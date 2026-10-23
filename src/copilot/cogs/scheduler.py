"""Durable scheduling commands.

Jobs can only start a read-only host-agent run or prepare a Content Studio
draft. They never post, publish, create Discord events or moderate anyone, and
scheduled public publishing has no job kind at all.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands

from ..agents.host import HostMode
from ..core.content_planning import CalendarKind
from ..core.content_studio import ContentLength, ContentType, DraftRequest
from ..core.scheduler import ScheduledJobView
from ..errors import ContentTargetError, ScheduleError
from ..ui import checks
from ..ui.content import DiscordContentSender
from ..ui.scheduler import scheduled_job_embed, scheduled_job_list_embed
from .base import CopilotCog
from .content import moderator_actor

log = logging.getLogger(__name__)

_STATUS_CHOICES = [
    app_commands.Choice(name="Pending", value="pending"),
    app_commands.Choice(name="Running", value="running"),
    app_commands.Choice(name="Succeeded", value="succeeded"),
    app_commands.Choice(name="Failed", value="failed"),
    app_commands.Choice(name="Cancelled", value="cancelled"),
]
_MODE_CHOICES = [
    app_commands.Choice(name="Community analysis", value="community"),
    app_commands.Choice(name="Web research", value="research"),
    app_commands.Choice(name="Draft preparation", value="draft_prep"),
    app_commands.Choice(name="Moderation review", value="moderation"),
    app_commands.Choice(name="Troubleshooting triage", value="triage"),
]
_CONTENT_TYPES = [
    app_commands.Choice(name="Announcement", value="announcement"),
    app_commands.Choice(name="Educational post", value="educational_post"),
    app_commands.Choice(name="Poll proposal", value="poll_proposal"),
    app_commands.Choice(name="Event announcement", value="event_announcement"),
]
_CALENDAR_CONTENT_TYPES = {
    CalendarKind.announcement: ContentType.announcement,
    CalendarKind.event: ContentType.event_announcement,
    CalendarKind.post: ContentType.educational_post,
    CalendarKind.reminder: ContentType.announcement,
}


class SchedulerCog(CopilotCog):
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._sender = DiscordContentSender(bot)

    @property
    def scheduler(self):
        return self.bot.scheduler

    async def _actor(self, interaction: discord.Interaction):
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return moderator_actor(interaction, self.settings)

    async def _target(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        guild_id = int(interaction.guild_id)
        chosen = channel or (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        if chosen is None:
            raise ContentTargetError("Choose a text channel for the scheduled draft.")
        if chosen.guild is None or chosen.guild.id != guild_id:
            raise ContentTargetError("The target channel must be in this server.")
        if not checks.channel_allowed(
            self.settings.channel_allowlist, chosen.id, chosen.parent_id
        ):
            raise ContentTargetError("The target channel is outside the configured allowlist.")
        target = await self._sender.resolve_target(guild_id, int(chosen.id))
        if not target.exists or not target.can_send:
            raise ContentTargetError()
        return chosen, target

    # --------------------------------------------------------------- scheduling

    @app_commands.command(
        name="schedule-agent",
        description="Moderator: schedule a read-only host-agent run (never posts)",
    )
    @app_commands.choices(mode=_MODE_CHOICES)
    @app_commands.describe(
        goal="What the run should investigate (read-only)",
        when="Local ISO date/time, e.g. 2026-11-05T18:00",
        timezone="IANA timezone name or UTC",
        mode="Optional bounded plan mode",
    )
    async def schedule_agent(
        self,
        interaction: discord.Interaction,
        goal: app_commands.Range[str, 3, 500],
        when: app_commands.Range[str, 10, 40],
        timezone: app_commands.Range[str, 1, 64] = "UTC",
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            context = self.skill_context(interaction)
            job, created = await self.scheduler.schedule_agent_run(
                context,
                goal=goal,
                run_at=when,
                timezone_name=timezone,
                mode=HostMode(mode.value) if mode is not None else None,
                idempotency_key=f"slash:{interaction.id}",
            )
            await self._reply_job(interaction, job, created)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="scheduling the agent run")

    @app_commands.command(
        name="schedule-digest",
        description="Moderator: schedule a community digest analysis (read-only, never posts)",
    )
    @app_commands.describe(
        when="Local ISO date/time, e.g. 2026-11-05T18:00",
        timezone="IANA timezone name or UTC",
        goal="Optional custom digest goal",
    )
    async def schedule_digest(
        self,
        interaction: discord.Interaction,
        when: app_commands.Range[str, 10, 40],
        timezone: app_commands.Range[str, 1, 64] = "UTC",
        goal: app_commands.Range[str, 0, 500] | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            context = self.skill_context(interaction)
            job, created = await self.scheduler.schedule_digest(
                context,
                run_at=when,
                timezone_name=timezone,
                goal=goal or "",
                idempotency_key=f"slash:{interaction.id}",
            )
            await self._reply_job(interaction, job, created)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="scheduling the digest")

    @app_commands.command(
        name="schedule-draft",
        description="Moderator: schedule a Content Studio draft (approval still required)",
    )
    @app_commands.choices(content_type=_CONTENT_TYPES)
    @app_commands.describe(
        topic="What the draft should be about",
        when="Local ISO date/time, e.g. 2026-11-05T18:00",
        timezone="IANA timezone name or UTC",
        content_type="Kind of content to draft",
        channel="Destination channel for the eventual approved post",
        audience="Optional audience description",
        tone="Optional tone",
        purpose="Optional purpose",
    )
    async def schedule_draft(
        self,
        interaction: discord.Interaction,
        topic: app_commands.Range[str, 3, 500],
        when: app_commands.Range[str, 10, 40],
        timezone: app_commands.Range[str, 1, 64] = "UTC",
        content_type: app_commands.Choice[str] | None = None,
        channel: discord.TextChannel | None = None,
        audience: app_commands.Range[str, 0, 200] | None = None,
        tone: app_commands.Range[str, 0, 100] | None = None,
        purpose: app_commands.Range[str, 0, 300] | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            _, target = await self._target(interaction, channel)
            selected = ContentType(content_type.value) if content_type else ContentType.announcement
            request = DraftRequest(
                content_type=selected,
                topic=topic,
                audience=audience or "",
                tone=tone or "",
                purpose=purpose or "",
                length=ContentLength.medium,
            )
            job, created = await self.scheduler.schedule_content_draft(
                actor,
                request,
                target=target,
                run_at=when,
                timezone_name=timezone,
                idempotency_key=f"slash:{interaction.id}",
            )
            await self._reply_job(interaction, job, created)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="scheduling the draft")

    @app_commands.command(
        name="calendar-schedule",
        description="Moderator: link a planned calendar entry to a durable scheduled job",
    )
    @app_commands.describe(
        entry_id="Calendar entry id",
        when="Local ISO date/time, e.g. 2026-11-05T18:00",
        timezone="IANA timezone name or UTC",
    )
    async def calendar_schedule(
        self,
        interaction: discord.Interaction,
        entry_id: int,
        when: app_commands.Range[str, 10, 40],
        timezone: app_commands.Range[str, 1, 64] = "UTC",
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            entry = await self.bot.content_planning.get_calendar_entry(actor, entry_id)
            if entry.state.value != "planned":
                raise ScheduleError("Only planned calendar entries can be scheduled.")
            if entry.scheduled_job_id is not None:
                existing = await self.scheduler.get_job(
                    guild_id=str(actor.guild_id), job_id=entry.scheduled_job_id
                )
                if existing.status in {"pending", "running"}:
                    raise ScheduleError(
                        f"This entry is already scheduled as job #{existing.job_id}."
                    )
            request = DraftRequest(
                content_type=_CALENDAR_CONTENT_TYPES[entry.kind],
                topic=entry.title,
                purpose=f"Scheduled {entry.kind.value} from calendar entry #{entry.entry_id}",
            )
            channel_id = entry.target_channel_id
            channel = (
                interaction.guild.get_channel(int(channel_id))
                if channel_id and interaction.guild is not None
                else None
            )
            chosen, target = await self._target(
                interaction, channel if isinstance(channel, discord.TextChannel) else None
            )
            _ = chosen
            job, created = await self.scheduler.schedule_content_draft(
                actor,
                request,
                target=target,
                run_at=when,
                timezone_name=timezone,
                idempotency_key=f"calendar:{entry.entry_id}:{interaction.id}",
            )
            await self.repo.link_calendar_entry_job(
                entry.entry_id,
                guild_id=str(actor.guild_id),
                job_id=job.job_id,
                updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            await self._reply_job(interaction, job, created)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="scheduling the calendar entry")

    # ------------------------------------------------------------------ control

    @app_commands.command(name="scheduled-jobs", description="Moderator: list durable scheduled jobs")
    @app_commands.choices(status=_STATUS_CHOICES)
    async def scheduled_jobs(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            jobs = await self.scheduler.list_jobs(
                guild_id=str(actor.guild_id),
                status=status.value if status is not None else None,
                limit=20,
            )
            await self.reply(interaction, embed=scheduled_job_list_embed(jobs))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="listing scheduled jobs")

    @app_commands.command(name="schedule-cancel", description="Moderator: cancel a pending scheduled job")
    async def schedule_cancel(self, interaction: discord.Interaction, job_id: int) -> None:
        actor = await self._actor(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            job = await self.scheduler.cancel_job(
                guild_id=str(actor.guild_id), job_id=job_id, actor_id=actor.actor_id
            )
            await self.reply(interaction, embed=scheduled_job_embed(job))
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="cancelling the scheduled job")

    async def _reply_job(
        self, interaction: discord.Interaction, job: ScheduledJobView, created: bool
    ) -> None:
        embed = scheduled_job_embed(job)
        if not created:
            embed.description = (embed.description or "") + "\n\nAn existing job with this key was reused."
        await self.reply(interaction, embed=embed)
