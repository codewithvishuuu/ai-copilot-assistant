"""Content Studio commands and the moderator approval inbox (Phase 2B).

Every command and button rebuilds the actor from Discord's current member state.
No draft, approval, target, or actor supplied by model output is trusted. The
publishing commands delegate to ``ContentPublisher``, which can only send after
the Phase 2A approval service claims the exact approved binding.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import discord
from discord import app_commands

from ..core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from ..core.content_studio import (
    ContentDraftView,
    ContentLength,
    ContentTarget,
    ContentType,
    DraftRequest,
    DraftStatus,
)
from ..core.content_voice import VoiceProfile, load_voice_profile, save_voice_profile
from ..core.publishing import PublishResult, PublishStatus
from ..errors import (
    ContentDraftConflictError,
    CopilotError,
    NotModeratorError,
    new_error_code,
)
from ..ui import checks
from ..ui.content import DiscordContentSender
from .base import CopilotCog

log = logging.getLogger(__name__)

_CONTENT_TYPES = [
    app_commands.Choice(name="Announcement", value="announcement"),
    app_commands.Choice(name="Educational post", value="educational_post"),
    app_commands.Choice(name="Poll proposal", value="poll_proposal"),
    app_commands.Choice(name="Event announcement", value="event_announcement"),
]

_LENGTH_CHOICES = [
    app_commands.Choice(name="Short (≤500 chars)", value="short"),
    app_commands.Choice(name="Medium (≤1000 chars)", value="medium"),
    app_commands.Choice(name="Long (server limit)", value="long"),
]

_EMOJI_CHOICES = [
    app_commands.Choice(name="None", value="none"),
    app_commands.Choice(name="Sparing", value="sparing"),
    app_commands.Choice(name="Allowed", value="allowed"),
]

_STATUS_CHOICES = [
    app_commands.Choice(name="Draft", value="draft"),
    app_commands.Choice(name="Pending approval", value="pending"),
    app_commands.Choice(name="Approved", value="approved"),
    app_commands.Choice(name="Rejected", value="rejected"),
    app_commands.Choice(name="Expired", value="expired"),
    app_commands.Choice(name="Cancelled", value="cancelled"),
    app_commands.Choice(name="Executing (needs recovery)", value="executing"),
    app_commands.Choice(name="Published", value="published"),
    app_commands.Choice(name="Failed", value="failed"),
]

_STATUS_COLORS = {
    DraftStatus.draft: discord.Color.light_grey(),
    DraftStatus.pending: discord.Color.gold(),
    DraftStatus.approved: discord.Color.green(),
    DraftStatus.rejected: discord.Color.red(),
    DraftStatus.expired: discord.Color.dark_grey(),
    DraftStatus.cancelled: discord.Color.dark_grey(),
    DraftStatus.executing: discord.Color.orange(),
    DraftStatus.published: discord.Color.blurple(),
    DraftStatus.failed: discord.Color.red(),
}

_ACTION_LABELS = {
    "edit": ("Edit", discord.ButtonStyle.secondary),
    "approve": ("Approve…", discord.ButtonStyle.success),
    "reject": ("Reject", discord.ButtonStyle.danger),
    "publish": ("Publish", discord.ButtonStyle.primary),
    "recover": ("Reconcile", discord.ButtonStyle.secondary),
}


def moderator_actor(interaction, settings) -> ApprovalActor:
    """Build a fresh actor from the current interaction member; never cache it."""
    guild_id = getattr(interaction, "guild_id", None)
    member = getattr(interaction, "user", None)
    if guild_id is None or member is None:
        raise NotModeratorError()
    permissions = getattr(member, "guild_permissions", None)
    roles = [role.id for role in getattr(member, "roles", []) if hasattr(role, "id")]
    if not checks.is_moderator(permissions, roles, settings.moderator_role_ids):
        raise NotModeratorError()
    return ApprovalActor(
        actor_id=str(member.id),
        guild_id=int(guild_id),
        kind=ActorKind.human,
        permissions=frozenset(
            {
                ApprovalPermission.propose,
                ApprovalPermission.decide,
                ApprovalPermission.cancel,
                ApprovalPermission.execute,
            }
        ),
    )


class ContentApprovalNoteModal(discord.ui.Modal, title="Approve exact draft version"):
    def __init__(self, cog: ContentCog, draft_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.draft_id = draft_id
        self.note = discord.ui.TextInput(
            label="Approval note (optional)",
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
        try:
            actor = moderator_actor(interaction, self.cog.settings)
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.cog.studio.approve_draft(
                actor, self.draft_id, note=str(self.note.value or "") or None
            )
            await self.cog._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001 - modal callbacks need their own safe reply
            await self.cog._safe_error(interaction, exc)


class ContentEditModal(discord.ui.Modal, title="Edit content draft"):
    def __init__(
        self,
        cog: ContentCog,
        *,
        draft_id: int,
        expected_version: int,
        content: str,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.draft_id = draft_id
        self.expected_version = expected_version
        self.content = discord.ui.TextInput(
            label=f"New text (creates v{expected_version + 1})",
            style=discord.TextStyle.paragraph,
            default=content[:4000],
            required=True,
            min_length=1,
            max_length=4000,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.cog.settings):
            return
        if not await checks.ensure_moderator(interaction, self.cog.settings):
            return
        try:
            actor = moderator_actor(interaction, self.cog.settings)
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.cog.studio.edit_draft(
                actor,
                self.draft_id,
                expected_version=self.expected_version,
                content=str(self.content.value or ""),
            )
            await self.cog._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001 - modal callbacks need their own safe reply
            await self.cog._safe_error(interaction, exc)


class ContentActionButton(discord.ui.Button):
    def __init__(self, *, draft_id: int, action: str) -> None:
        label, style = _ACTION_LABELS[action]
        super().__init__(
            label=label,
            style=style,
            custom_id=f"content:{action}:{draft_id}",
            row=0,
        )
        self.action = action
        self.draft_id = draft_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ContentActionView):
            return
        if self.action == "edit":
            await view.open_edit_modal(interaction)
        elif self.action == "approve":
            await view.open_approve_modal(interaction)
        else:
            await view.handle(interaction, self.draft_id, self.action)


class ContentActionView(discord.ui.View):
    """Short-lived inbox buttons. Slash commands remain the durable path."""

    def __init__(
        self,
        cog: ContentCog,
        view: ContentDraftView,
        *,
        actions: Sequence[str],
    ) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.draft = view
        for action in actions:
            self.add_item(ContentActionButton(draft_id=view.draft_id, action=action))

    async def handle(self, interaction: discord.Interaction, draft_id: int, action: str) -> None:
        await self.cog.perform_action(interaction, draft_id, action)

    async def open_approve_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ContentApprovalNoteModal(self.cog, self.draft.draft_id)
        )

    async def open_edit_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ContentEditModal(
                self.cog,
                draft_id=self.draft.draft_id,
                expected_version=self.draft.version,
                content=self.draft.content,
            )
        )


def _actions_for(view: ContentDraftView) -> list[str]:
    actions: list[str] = []
    if view.can_edit:
        actions.append("edit")
    if view.can_approve:
        actions.append("approve")
    if view.can_reject:
        actions.append("reject")
    if view.can_publish:
        actions.append("publish")
    if view.can_recover:
        actions.append("recover")
    return actions


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def voice_embed(voice: VoiceProfile) -> discord.Embed:
    embed = discord.Embed(
        title="Community content voice",
        color=discord.Color.blurple(),
        description="Guild-scoped style guidance injected as untrusted data. It never overrides safety rules.",
    )
    embed.add_field(name="Tone", value=_clip(voice.tone or "not set", 256), inline=True)
    embed.add_field(name="Audience", value=_clip(voice.audience or "not set", 256), inline=True)
    embed.add_field(name="Emoji", value=voice.emoji_policy, inline=True)
    embed.add_field(name="Formatting", value=_clip(voice.formatting or "not set", 256), inline=False)
    if voice.style_notes:
        embed.add_field(name="Style notes", value=_clip(voice.style_notes, 1024), inline=False)
    if voice.banned_phrases:
        embed.add_field(
            name="Banned phrases",
            value=_clip(", ".join(voice.banned_phrases), 1024),
            inline=False,
        )
    if voice.updated_by:
        embed.set_footer(text=f"Last updated by {voice.updated_by}")
    return embed


def draft_embed(view: ContentDraftView) -> discord.Embed:
    embed = discord.Embed(
        title=f"Content draft {view.draft_id} · v{view.version}",
        description=_clip(view.content, 4096),
        color=_STATUS_COLORS[view.status],
    )
    embed.add_field(name="Status", value=view.status.value, inline=True)
    embed.add_field(name="Target", value=f"<#{view.target_channel_id}>", inline=True)
    embed.add_field(name="Type", value=view.content_type.value, inline=True)
    embed.add_field(name="Topic", value=_clip(view.topic, 1024), inline=False)
    if view.audience or view.tone or view.purpose:
        details = "\n".join(
            part
            for part in (
                f"Audience: {view.audience}" if view.audience else "",
                f"Tone: {view.tone}" if view.tone else "",
                f"Purpose: {view.purpose}" if view.purpose else "",
            )
            if part
        )
        embed.add_field(name="Brief", value=_clip(details, 1024), inline=False)
    errors = view.validation_errors()
    warnings = view.validation_warnings()
    if errors:
        embed.add_field(
            name="Blocking findings",
            value=_clip("\n".join(f"• {item.message}" for item in errors), 1024),
            inline=False,
        )
    if warnings:
        embed.add_field(
            name="Warnings",
            value=_clip("\n".join(f"• {item.message}" for item in warnings), 1024),
            inline=False,
        )
    approval = "none"
    if view.approval_id is not None:
        approval = f"#{view.approval_id} · {view.approval_state or 'unknown'}"
        if view.approval_expires_at:
            approval += f"\nexpires {view.approval_expires_at}"
    embed.add_field(name="Approval", value=_clip(approval, 1024), inline=False)
    if view.execution_state:
        execution = view.execution_state
        if view.execution_started_at:
            execution += f" · started {view.execution_started_at}"
        if view.published_message_id:
            execution += f"\nmessage id {view.published_message_id}"
        embed.add_field(name="Execution", value=_clip(execution, 1024), inline=False)
    embed.set_footer(text=(f"Author {view.author_id} · hash {view.content_hash[:12]} · exact version shown above"))
    return embed


def list_embed(views: Sequence[ContentDraftView], *, status: str | None) -> discord.Embed:
    title = "Content Studio inbox" if status is None else f"Content Studio · {status}"
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    if not views:
        embed.description = "No drafts match this view."
        return embed
    for view in views[:25]:
        embed.add_field(
            name=f"#{view.draft_id} · {view.status.value} · v{view.version}",
            value=_clip(f"{view.topic}\nTarget: <#{view.target_channel_id}>", 1024),
            inline=False,
        )
    embed.set_footer(text="Open one with /content-view; approval buttons are rechecked server-side.")
    return embed


def publish_embed(result: PublishResult) -> discord.Embed:
    if result.status is PublishStatus.published:
        embed = discord.Embed(
            title="Content published" if result.recorded else "Content sent — record incomplete",
            color=discord.Color.green() if result.recorded else discord.Color.orange(),
            description=(
                f"Discord confirmed message id `{result.message_id}`."
                if result.recorded
                else (
                    "Discord confirmed the message, but the local execution record is "
                    "incomplete. Do **not** resend. Use `/content-recover`."
                )
            ),
        )
    elif result.status is PublishStatus.ambiguous:
        embed = discord.Embed(
            title="Publication outcome uncertain",
            color=discord.Color.orange(),
            description=(
                (result.detail or "Discord did not confirm the send.")
                + "\n\nDo **not** retry. Use `/content-recover`; it never re-sends."
            ),
        )
    else:
        embed = discord.Embed(
            title="Publication failed",
            color=discord.Color.red(),
            description=(result.detail or "Nothing was published.")
            + (f"\nError code: `{result.error_code}`" if result.error_code else ""),
        )
    if result.execution_id is not None:
        embed.add_field(name="Execution", value=str(result.execution_id), inline=True)
    if result.message_id:
        embed.add_field(name="Message id", value=f"`{result.message_id}`", inline=True)
    if not result.recorded:
        embed.add_field(name="Recorded", value="no", inline=True)
    return embed


class ContentCog(CopilotCog):
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._sender = DiscordContentSender(bot)

    @property
    def studio(self):
        return self.bot.content_studio

    @property
    def publisher(self):
        return self.bot.content_publisher

    async def _gate(self, interaction: discord.Interaction) -> ApprovalActor | None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return moderator_actor(interaction, self.settings)

    async def _target_for(self, interaction: discord.Interaction, channel: discord.TextChannel) -> ContentTarget:
        return await self._sender.resolve_target(int(interaction.guild_id), int(channel.id))

    async def _reply_view(self, interaction: discord.Interaction, view: ContentDraftView) -> None:
        actions = _actions_for(view)
        ui_view = ContentActionView(self, view, actions=actions) if actions else None
        await self.reply(interaction, embed=draft_embed(view), view=ui_view)

    async def perform_action(self, interaction: discord.Interaction, draft_id: int, action: str) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if action == "approve":
                view = await self.studio.approve_draft(actor, draft_id)
                await self._reply_view(interaction, view)
            elif action == "reject":
                view = await self.studio.reject_draft(actor, draft_id)
                await self._reply_view(interaction, view)
            elif action == "publish":
                result = await self.publisher.publish(actor, draft_id, sender=self._sender)
                await self.reply(
                    interaction,
                    embed=publish_embed(result),
                    view=await self._result_view(draft_id, result, actor),
                )
            elif action == "recover":
                result = await self.publisher.recover(actor, draft_id, sender=self._sender)
                await self.reply(
                    interaction,
                    embed=publish_embed(result),
                    view=await self._result_view(draft_id, result, actor),
                )
            else:  # pragma: no cover - buttons are built from a fixed map
                raise ContentDraftConflictError("Unknown content action.")
        except Exception as exc:  # noqa: BLE001 - view callbacks need their own safe error reply
            await self._safe_error(interaction, exc)

    async def _result_view(
        self, draft_id: int, result: PublishResult, actor: ApprovalActor
    ) -> ContentActionView | None:
        if result.status is not PublishStatus.ambiguous:
            return None
        try:
            view = await self.studio.get_draft(actor, draft_id)
        except Exception:  # noqa: BLE001 - recovery stays available as a slash command
            return None
        actions = [action for action in _actions_for(view) if action == "recover"]
        return ContentActionView(self, view, actions=actions) if actions else None

    async def _safe_error(self, interaction: discord.Interaction, exc: BaseException) -> None:
        code = new_error_code()
        if isinstance(exc, CopilotError):
            message = exc.user_message()
            log.info("Content action error %s: %s", code, exc)
            if exc.detail:
                log.debug("Content action error %s detail: %s", code, exc.detail)
        else:
            message = "Unexpected error while handling this content action."
            log.exception("Unhandled content action error %s: %s", code, exc)
        try:
            await self.reply(interaction, content=f"{message}\n`{code}`")
        except Exception:  # pragma: no cover - interaction may be gone
            log.warning("Could not deliver content action error %s", code)

    # ----------------------------------------------------------------- commands

    @app_commands.command(
        name="content-create",
        description="Moderator: generate a community content draft for human review",
    )
    @app_commands.choices(content_type=_CONTENT_TYPES, length=_LENGTH_CHOICES, emoji_policy=_EMOJI_CHOICES)
    @app_commands.describe(
        content_type="What kind of content to draft",
        topic="Explicit topic or brief",
        channel="Intended public channel (defaults to this channel)",
        audience="Who this is for",
        tone="Requested tone",
        purpose="Why this is being posted",
        length="Requested length",
        formatting="Requested formatting (e.g. bullets, headers, no links)",
        emoji_policy="How many emoji to use",
    )
    async def content_create(
        self,
        interaction: discord.Interaction,
        content_type: app_commands.Choice[str],
        topic: app_commands.Range[str, 3, 500],
        channel: discord.TextChannel | None = None,
        audience: str | None = None,
        tone: str | None = None,
        purpose: str | None = None,
        length: app_commands.Choice[str] | None = None,
        formatting: str | None = None,
        emoji_policy: app_commands.Choice[str] | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await self.reply(interaction, content="Choose a normal text channel as the target.")
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            target = await self._target_for(interaction, target_channel)
            request = DraftRequest(
                content_type=ContentType(content_type.value),
                topic=topic,
                audience=audience or "",
                tone=tone or "",
                purpose=purpose or "",
                length=ContentLength(length.value if length is not None else "medium"),
                formatting=formatting or "",
                emoji_policy=emoji_policy.value if emoji_policy is not None else "",
            )
            view = await self.studio.generate_draft(actor, request, target=target, provider=self.provider)
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-list",
        description="Moderator: list Content Studio drafts and pending approvals",
    )
    @app_commands.choices(status=_STATUS_CHOICES)
    async def content_list(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            wanted = DraftStatus(status.value) if status is not None else None
            views = await self.studio.list_drafts(actor, status=wanted, limit=25)
            await self.reply(
                interaction,
                embed=list_embed(views, status=status.value if status is not None else None),
            )
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-view",
        description="Moderator: view one exact draft version, target, findings and approval",
    )
    async def content_view(self, interaction: discord.Interaction, draft_id: int) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            view = await self.studio.get_draft(actor, draft_id)
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-edit",
        description="Moderator: create a new immutable version of a draft (invalidates old approval)",
    )
    @app_commands.describe(
        draft_id="Draft to edit",
        expected_version="Version you are editing (shown by /content-view)",
        content="Replacement text (omit to keep text)",
        channel="Replacement target channel (omit to keep target)",
    )
    async def content_edit(
        self,
        interaction: discord.Interaction,
        draft_id: int,
        expected_version: app_commands.Range[int, 1, 100000],
        content: app_commands.Range[str, 1, 4000] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        if content is None and channel is None:
            await self.reply(interaction, content="Provide new content, a new target channel, or both.")
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            target = await self._target_for(interaction, channel) if channel is not None else None
            view = await self.studio.edit_draft(
                actor,
                draft_id,
                expected_version=expected_version,
                content=content,
                target=target,
            )
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-submit",
        description="Moderator: request human approval for the exact current draft version",
    )
    async def content_submit(self, interaction: discord.Interaction, draft_id: int) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.studio.get_draft(actor, draft_id)
            target = await self._sender.resolve_target(draft.guild_id, draft.target_channel_id)
            view = await self.studio.submit_draft(actor, draft_id, target=target)
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-approve",
        description="Moderator: approve a pending content draft (creator cannot self-approve)",
    )
    async def content_approve(
        self,
        interaction: discord.Interaction,
        draft_id: int,
        note: str | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.studio.approve_draft(actor, draft_id, note=note)
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-reject",
        description="Moderator: reject a pending content draft with an optional note",
    )
    async def content_reject(
        self,
        interaction: discord.Interaction,
        draft_id: int,
        note: str | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.studio.reject_draft(actor, draft_id, note=note)
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-cancel",
        description="Moderator: cancel a live content approval request",
    )
    async def content_cancel(
        self,
        interaction: discord.Interaction,
        draft_id: int,
        reason: str | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            view = await self.studio.cancel_draft(actor, draft_id, reason=reason)
            await self._reply_view(interaction, view)
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-publish",
        description="Moderator: publish an approved draft through the approval boundary",
    )
    async def content_publish(self, interaction: discord.Interaction, draft_id: int) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.publisher.publish(actor, draft_id, sender=self._sender)
            await self.reply(
                interaction,
                embed=publish_embed(result),
                view=await self._result_view(draft_id, result, actor),
            )
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-recover",
        description="Moderator: reconcile an uncertain send; proves the message exists, never re-sends",
    )
    async def content_recover(self, interaction: discord.Interaction, draft_id: int) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.publisher.recover(actor, draft_id, sender=self._sender)
            await self.reply(
                interaction,
                embed=publish_embed(result),
                view=await self._result_view(draft_id, result, actor),
            )
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    # ------------------------------------------------------------- voice memory

    @app_commands.command(
        name="voice-show",
        description="Moderator: show this server's content voice and style preferences",
    )
    async def voice_show(self, interaction: discord.Interaction) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            voice = await load_voice_profile(self.repo, actor.guild_id)
            await self.reply(interaction, embed=voice_embed(voice))
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="voice-set",
        description="Moderator: update this server's content voice (omitted fields are kept)",
    )
    @app_commands.choices(emoji_policy=_EMOJI_CHOICES)
    @app_commands.describe(
        tone="Preferred tone",
        audience="Default audience",
        emoji_policy="Emoji policy",
        formatting="Preferred formatting",
        style_notes="Additional style guidance",
        banned_phrases="Comma-separated phrases the writer must avoid",
        clear="Reset the whole voice profile",
    )
    async def voice_set(
        self,
        interaction: discord.Interaction,
        tone: app_commands.Range[str, 1, 100] | None = None,
        audience: app_commands.Range[str, 1, 200] | None = None,
        emoji_policy: app_commands.Choice[str] | None = None,
        formatting: app_commands.Range[str, 1, 200] | None = None,
        style_notes: app_commands.Range[str, 1, 1500] | None = None,
        banned_phrases: app_commands.Range[str, 1, 2000] | None = None,
        clear: bool = False,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            current = await load_voice_profile(self.repo, actor.guild_id)
            if clear:
                voice = await save_voice_profile(
                    self.repo,
                    guild_id=actor.guild_id,
                    updated_by=actor.actor_id,
                    tone="",
                    audience="",
                    emoji_policy="sparing",
                    formatting="",
                    style_notes="",
                    banned_phrases=[],
                    now=_now_iso(),
                )
            else:
                phrases = (
                    [item.strip() for item in banned_phrases.split(",") if item.strip()]
                    if banned_phrases is not None
                    else list(current.banned_phrases)
                )
                voice = await save_voice_profile(
                    self.repo,
                    guild_id=actor.guild_id,
                    updated_by=actor.actor_id,
                    tone=current.tone if tone is None else tone,
                    audience=current.audience if audience is None else audience,
                    emoji_policy=(emoji_policy.value if emoji_policy is not None else current.emoji_policy),
                    formatting=current.formatting if formatting is None else formatting,
                    style_notes=current.style_notes if style_notes is None else style_notes,
                    banned_phrases=phrases,
                    now=_now_iso(),
                )
            await self.repo.audit(
                actor_id=actor.actor_id,
                action="content.voice_updated",
                target_type="guild",
                target_id=str(actor.guild_id),
                guild_id=str(actor.guild_id),
                meta={
                    "guild_id": str(actor.guild_id),
                    "cleared": clear,
                    "emoji_policy": voice.emoji_policy,
                    "banned_phrase_count": len(voice.banned_phrases),
                },
            )
            await self.reply(interaction, embed=voice_embed(voice))
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="content-feedback",
        description="Moderator: record style feedback that future drafts should reuse",
    )
    @app_commands.describe(
        feedback="What the writer should do differently",
        draft_id="Optional draft this feedback refers to",
        tags="Optional comma-separated labels",
    )
    async def content_feedback(
        self,
        interaction: discord.Interaction,
        feedback: app_commands.Range[str, 3, 1000],
        draft_id: int | None = None,
        tags: str | None = None,
    ) -> None:
        actor = await self._gate(interaction)
        if actor is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            version: int | None = None
            if draft_id is not None:
                draft = await self.studio.get_draft(actor, draft_id)
                version = draft.version
            labels = (
                [item.strip()[:40] for item in tags.split(",") if item.strip()][:10]
                if tags
                else []
            )
            feedback_id = await self.repo.add_content_feedback(
                guild_id=str(actor.guild_id),
                draft_id=draft_id,
                version=version,
                feedback=feedback,
                tags_json=json.dumps(labels),
                created_by=actor.actor_id,
                created_at=_now_iso(),
            )
            await self.repo.audit(
                actor_id=actor.actor_id,
                action="content.feedback_added",
                target_type="content_feedback",
                target_id=str(feedback_id),
                guild_id=str(actor.guild_id),
                meta={
                    "guild_id": str(actor.guild_id),
                    "draft_id": draft_id,
                    "version": version,
                    "tags": labels,
                },
            )
            await self.reply(
                interaction,
                content=(
                    f"Feedback `#{feedback_id}` saved. Future drafts in this server will reuse it "
                    "as untrusted style guidance; it never changes approval policy."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await self._safe_error(interaction, exc)
