"""AI moderation listener, guild policy commands, and private review flow."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..core.moderation_ai import (
    DEFAULT_CATEGORIES,
    IncidentReport,
    ModerationOutcome,
    ModerationPolicy,
    ModerationService,
)
from ..errors import NotAllowedError
from ..ui import checks
from ..util.logging import mask_id
from ..util.text import truncate
from .base import CopilotCog

if TYPE_CHECKING:
    from ..bot import CommunityCopilot

log = logging.getLogger(__name__)

_MAX_SEEN_MESSAGE_IDS = 512
_CONTEXT_MESSAGES = 6
_CONTEXT_CHARS = 500

_SEVERITY_CHOICES = [
    app_commands.Choice(name="Low", value="low"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="High", value="high"),
    app_commands.Choice(name="Critical", value="critical"),
]
_REVIEW_CHOICES = [
    app_commands.Choice(name="Confirm (incident was real)", value="confirm"),
    app_commands.Choice(name="Dismiss (false positive)", value="dismiss"),
    app_commands.Choice(name="Override (moderator judgment)", value="override"),
]


class AutomodCog(CopilotCog):
    def __init__(self, bot: CommunityCopilot) -> None:
        super().__init__(bot)
        self.service: ModerationService | None = getattr(bot, "moderation_service", None)
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._context: dict[int, deque[str]] = {}
        self._gate = asyncio.Semaphore(1)

    # ---------------------------------------------------------------- listener

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._handle(message)
        except Exception:  # noqa: BLE001 - moderation must never kill the event loop
            log.exception("moderation: listener crashed")

    async def _handle(self, message) -> None:
        settings = self.settings
        if self.service is None or not settings.moderation_enabled:
            return
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        if getattr(author, "bot", False) or guild is None:
            return
        if getattr(message, "webhook_id", None) is not None:
            return
        if not checks.guild_allowed(settings.guild_allowlist, getattr(guild, "id", None)):
            return
        if not checks.channel_allowed(
            settings.channel_allowlist,
            getattr(channel, "id", None),
            getattr(channel, "parent_id", None),
        ):
            return

        # The privileged Message Content intent stays off, so Discord only
        # delivers text for messages that mention the bot. Enforce that scope
        # explicitly: ordinary channel messages are not assessed, and this
        # listener must never imply otherwise.
        if not self._mentions_bot(message):
            return
        content = getattr(message, "content", "") or ""
        if not content.strip():
            # Defensive: a mention with no readable text cannot be assessed.
            return

        message_id = getattr(message, "id", None)
        if message_id is None:
            return
        if self._remember(message_id):
            log.debug("moderation: duplicate gateway delivery ignored (id=%s)", mask_id(message_id))
            return

        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return
        context = tuple(self._context.get(int(channel_id), ()))
        if channel_id is not None:
            buffer = self._context.setdefault(int(channel_id), deque(maxlen=_CONTEXT_MESSAGES))
            buffer.append(truncate(content, _CONTEXT_CHARS))
            if len(self._context) > 2_000:
                self._context.pop(next(iter(self._context)))

        async with self._gate:
            async def _delete() -> None:
                await message.delete()

            outcome = await self.service.evaluate(
                guild_id=guild.id,
                channel_id=channel_id,
                author_id=author.id,
                message_id=message_id,
                content=content,
                context=context,
                delete_callback=_delete,
            )
        if outcome.report is not None:
            await self._deliver_report(outcome.report)
        if outcome.warning_created and outcome.notify_user:
            await self._notify_author(author, outcome)
        if outcome.created:
            log.info(
                "moderation: incident %s action=%s category=%s guild=%s message=%s",
                outcome.incident_id,
                outcome.action_taken,
                outcome.report.category if outcome.report else "?",
                mask_id(getattr(guild, "id", None)),
                mask_id(message_id),
            )

    def _mentions_bot(self, message) -> bool:
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        if bot_id is None:
            return False
        mentions = getattr(message, "mentions", ()) or ()
        return any(getattr(user, "id", None) == bot_id for user in mentions)

    def _remember(self, message_id: int) -> bool:
        """True when this id was already handled in this process."""
        key = int(message_id)
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > _MAX_SEEN_MESSAGE_IDS:
            self._seen.popitem(last=False)
        return False

    # --------------------------------------------------------------- reporting

    async def _deliver_report(self, report: IncidentReport) -> None:
        if self.service is None:
            return
        try:
            policy = await self.service.policy(report.guild_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("moderation: policy read for report failed (%s)", type(exc).__name__)
            return
        if not policy.report_channel_id:
            log.warning(
                "moderation: no report channel configured for guild %s; incident %s stored only",
                mask_id(report.guild_id),
                report.incident_id,
            )
            await self.service.mark_report_delivered(
                incident_id=report.incident_id, guild_id=report.guild_id, delivered=False
            )
            return
        channel = await self._report_channel(report.guild_id, policy.report_channel_id)
        if channel is None:
            await self.service.mark_report_delivered(
                incident_id=report.incident_id, guild_id=report.guild_id, delivered=False
            )
            return
        embed = _report_embed(report)
        try:
            await channel.send(
                embed=embed,
                view=ModerationReviewView(self, report.incident_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:  # noqa: BLE001 - delivery failure is reported honestly
            log.warning(
                "moderation: report delivery failed for incident %s (%s)",
                report.incident_id,
                type(exc).__name__,
            )
            await self.service.mark_report_delivered(
                incident_id=report.incident_id, guild_id=report.guild_id, delivered=False
            )
            return
        await self.service.mark_report_delivered(
            incident_id=report.incident_id, guild_id=report.guild_id, delivered=True
        )

    async def _report_channel(self, guild_id: str, channel_id: str):
        getter = getattr(self.bot, "get_channel", None)
        channel = getter(int(channel_id)) if callable(getter) else None
        if channel is None:
            fetcher = getattr(self.bot, "fetch_channel", None)
            if callable(fetcher):
                try:
                    channel = await fetcher(int(channel_id))
                except Exception as exc:  # noqa: BLE001
                    log.warning("moderation: report channel fetch failed (%s)", type(exc).__name__)
                    return None
        if channel is None:
            return None
        channel_guild = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild is not None and str(channel_guild) != str(guild_id):
            log.warning("moderation: report channel belongs to a different guild; refusing")
            return None
        if not callable(getattr(channel, "send", None)):
            return None
        return channel

    async def _notify_author(self, author, outcome: ModerationOutcome) -> None:
        if outcome.report is None:
            return
        send = getattr(author, "send", None)
        if not callable(send):
            return
        try:
            await send(
                "A moderator will review one of your recent messages in this community "
                f"(category: {outcome.report.category}). This is an automated notice, not a ban."
            )
        except Exception as exc:  # noqa: BLE001 - DMs are best-effort
            log.info("moderation: author notice not delivered (%s)", type(exc).__name__)

    # --------------------------------------------------------------- commands

    @app_commands.command(
        name="moderation-policy",
        description="Moderator: configure AI moderation for this server",
    )
    @app_commands.choices(severity=_SEVERITY_CHOICES)
    @app_commands.describe(
        enabled="Turn AI moderation on or off",
        report_channel="Private moderator channel for incident reports",
        delete_enabled="Allow automatic deletion (only above thresholds)",
        severity="Minimum severity for automatic action",
        confidence="Minimum model confidence for automatic action (0-1)",
        categories="Comma-separated categories (harassment, abuse, threats, hate, spam, other)",
        max_per_day="Maximum incidents per author per day before autos are paused",
        notify_user="Send the author a neutral DM notice (no timeout or ban)",
        clear_report_channel="Remove the configured report channel",
    )
    async def policy_command(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        report_channel: discord.TextChannel | None = None,
        delete_enabled: bool | None = None,
        severity: app_commands.Choice[str] | None = None,
        confidence: float | None = None,
        categories: str | None = None,
        max_per_day: int | None = None,
        notify_user: bool | None = None,
        clear_report_channel: bool = False,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        if self.service is None:
            await self.reply(interaction, content="AI moderation is not available on this bot.")
            return
        if not self.settings.moderation_enabled:
            await self.reply(
                interaction,
                content=(
                    "AI moderation is disabled globally by configuration "
                    "(`MODERATION_ENABLED=false`). The policy was not changed."
                ),
            )
            return
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            await self.reply(interaction, content=str(NotAllowedError()))
            return
        if report_channel is not None:
            target_guild = getattr(getattr(report_channel, "guild", None), "id", None)
            if target_guild is not None and int(target_guild) != int(guild_id):
                await self.reply(interaction, content="The report channel must be in this server.")
                return
        await interaction.response.defer(ephemeral=True)
        try:
            # Omitted options preserve the existing policy. The old defaults made
            # `/moderation-policy enabled:true` silently wipe the report channel
            # and every other setting.
            existing = None
            reader = getattr(self.service, "policy", None)
            if callable(reader):
                existing = await reader(guild_id)
            selected_categories = (
                list(existing.categories)
                if categories is None and existing is not None
                else [
                    item.strip().lower()
                    for item in str(categories if categories is not None else ",".join(DEFAULT_CATEGORIES)).split(",")
                    if item.strip()
                ]
            )
            if clear_report_channel:
                report_channel_id = None
            elif report_channel is not None:
                report_channel_id = report_channel.id
            elif existing is not None:
                report_channel_id = existing.report_channel_id
            else:
                report_channel_id = None
            effective = await self.service.update_policy(
                guild_id=guild_id,
                actor_id=str(interaction.user.id),
                enabled=bool(enabled),
                report_channel_id=report_channel_id,
                delete_enabled=(
                    existing.delete_enabled
                    if delete_enabled is None and existing is not None
                    else bool(delete_enabled)
                ),
                severity_threshold=(
                    severity.value
                    if severity is not None
                    else (
                        existing.severity_threshold
                        if existing is not None
                        else self.settings.moderation_default_severity
                    )
                ),
                confidence_threshold=float(
                    existing.confidence_threshold
                    if confidence is None and existing is not None
                    else (
                        self.settings.moderation_default_confidence
                        if confidence is None
                        else confidence
                    )
                ),
                categories=selected_categories,
                max_incidents_per_day=int(
                    existing.max_incidents_per_day
                    if max_per_day is None and existing is not None
                    else (20 if max_per_day is None else max_per_day)
                ),
                notify_user=(
                    existing.notify_user
                    if notify_user is None and existing is not None
                    else bool(notify_user)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - typed errors are already user-safe
            await self.handle_error(interaction, exc, action="updating the moderation policy")
            return
        await self.reply(interaction, content=_policy_summary(effective))

    @app_commands.command(
        name="moderation-status",
        description="Moderator: show this server's AI moderation policy and recent incidents",
    )
    async def status_command(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        guild_id = getattr(interaction, "guild_id", None)
        if self.service is None or guild_id is None:
            await self.reply(interaction, content="AI moderation is not available on this bot.")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            policy = await self.service.policy(guild_id)
            counts = await self.service.counts(guild_id=guild_id)
            recent = await self.service.list_incidents(guild_id=guild_id, limit=5)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="reading moderation status")
            return
        lines = [
            f"**AI moderation:** {'enabled' if policy.enabled else 'disabled'} "
            f"(global switch: {'on' if self.settings.moderation_enabled else 'off'})",
            f"Report channel: {f'<#{policy.report_channel_id}>' if policy.report_channel_id else 'not configured'}",
            f"Delete automatically: {'yes' if policy.delete_enabled else 'no'}",
            f"Thresholds: severity ≥ **{policy.severity_threshold}**, "
            f"confidence ≥ **{policy.confidence_threshold:.2f}**",
            f"Categories: {', '.join(policy.categories)}",
            f"Incidents: {counts['open']} open, {counts['confirmed']} confirmed, "
            f"{counts['dismissed']} dismissed, {counts['overridden']} overridden",
        ]
        if recent:
            lines.append("**Recent incidents**")
            for row in recent:
                link = (
                    f"https://discord.com/channels/{guild_id}/{row['channel_id']}/{row['message_id']}"
                )
                lines.append(
                    f"`#{row['id']}` {row['category']}/{row['severity']} "
                    f"conf={float(row['confidence']):.2f} action={row['action_taken']} "
                    f"state={row['state']} — <{link}>"
                )
        else:
            lines.append("No incidents recorded.")
        await self.reply(interaction, content="\n".join(lines))

    @app_commands.command(
        name="moderation-review",
        description="Moderator: confirm, dismiss, or override a stored moderation incident",
    )
    @app_commands.choices(decision=_REVIEW_CHOICES)
    @app_commands.describe(incident_id="Incident id from a report or /moderation-status", note="Optional reviewer note")
    async def review_command(
        self,
        interaction: discord.Interaction,
        incident_id: int,
        decision: app_commands.Choice[str],
        note: str | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        if self.service is None:
            await self.reply(interaction, content="AI moderation is not available on this bot.")
            return
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            await self.reply(interaction, content=str(NotAllowedError()))
            return
        # Ack before the state change; review is idempotent only in the sense
        # that a second attempt reports a conflict instead of overwriting.
        await interaction.response.defer(ephemeral=True)
        try:
            row = await self.service.review(
                incident_id=int(incident_id),
                guild_id=guild_id,
                reviewer_id=str(interaction.user.id),
                decision=decision.value,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="reviewing the moderation incident")
            return
        await self.reply(
            interaction,
            content=f"Incident `#{row['id']}` marked **{row['state']}**. No ban or timeout was applied.",
        )


class ModerationReviewView(discord.ui.View):
    """Buttons on a private report; each opens a note modal before recording."""

    def __init__(self, cog: AutomodCog, incident_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.incident_id = incident_id

    async def _open(self, interaction: discord.Interaction, decision: str) -> None:
        await interaction.response.send_modal(
            ModerationNoteModal(self.cog, self.incident_id, decision)
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "confirm")

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "dismiss")

    @discord.ui.button(label="Override", style=discord.ButtonStyle.danger)
    async def override(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, "override")


class ModerationNoteModal(discord.ui.Modal, title="Moderation review"):
    def __init__(self, cog: AutomodCog, incident_id: int, decision: str) -> None:
        super().__init__()
        self.cog = cog
        self.incident_id = incident_id
        self.decision = decision
        self.note = discord.ui.TextInput(
            label="Reviewer note (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.cog.settings):
            return
        if not await checks.ensure_moderator(interaction, self.cog.settings):
            return
        guild_id = getattr(interaction, "guild_id", None)
        if self.cog.service is None or guild_id is None:
            await interaction.response.send_message(
                "AI moderation is not available.", ephemeral=True
            )
            return
        try:
            row = await self.cog.service.review(
                incident_id=self.incident_id,
                guild_id=guild_id,
                reviewer_id=str(interaction.user.id),
                decision=self.decision,
                note=str(self.note.value or "") or None,
            )
        except Exception as exc:  # noqa: BLE001
            await self.cog.handle_error(interaction, exc, action="reviewing the moderation incident")
            return
        await interaction.response.send_message(
            f"Incident `#{row['id']}` marked **{row['state']}**.", ephemeral=True
        )


def _policy_summary(policy: ModerationPolicy) -> str:
    lines = [
        f"AI moderation is now **{'enabled' if policy.enabled else 'disabled'}**.",
        f"Report channel: {f'<#{policy.report_channel_id}>' if policy.report_channel_id else 'not configured'}",
        f"Automatic deletion: {'on' if policy.delete_enabled else 'off'}",
        f"Thresholds: severity ≥ **{policy.severity_threshold}**, "
        f"confidence ≥ **{policy.confidence_threshold:.2f}**",
        f"Categories: {', '.join(policy.categories)}",
        f"Daily per-author action limit: {policy.max_incidents_per_day}",
        "No automatic ban, kick, or timeout is ever applied.",
    ]
    if policy.enabled and not policy.report_channel_id:
        lines.append("Set a report channel before relying on this policy; incidents still store.")
    return "\n".join(lines)


def _report_embed(report: IncidentReport) -> discord.Embed:
    colors = {
        "low": discord.Color.blue(),
        "medium": discord.Color.gold(),
        "high": discord.Color.orange(),
        "critical": discord.Color.red(),
    }
    embed = discord.Embed(
        title=f"AI moderation incident #{report.incident_id}",
        description="Model assessment for human review; review the linked message in Discord.",
        color=colors.get(report.severity, discord.Color.orange()),
    )
    embed.add_field(name="Category", value=report.category, inline=True)
    embed.add_field(name="Severity", value=report.severity, inline=True)
    embed.add_field(name="Confidence", value=f"{report.confidence:.2f}", inline=True)
    embed.add_field(name="Action taken", value=report.action_taken, inline=True)
    embed.add_field(name="Repeat incidents", value=str(report.repeat_count), inline=True)
    if report.delete_failed:
        embed.add_field(name="Delete failed", value="Missing permission or action limit", inline=True)
    if report.warning_created:
        embed.add_field(name="Warning recorded", value="yes", inline=True)
    embed.add_field(
        name="Recommended next step",
        value=truncate(report.recommended_next_step or "Moderator review.", 1000),
        inline=False,
    )
    if report.message_url:
        embed.add_field(name="Message", value=f"[Open in Discord]({report.message_url})", inline=False)
    embed.set_footer(text="Confirm, dismiss, or override with the buttons below. Review is private.")
    return embed
