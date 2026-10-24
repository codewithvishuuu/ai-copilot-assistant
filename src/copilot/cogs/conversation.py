"""Mention-triggered conversation (Phase 1) with persistent context (Phase 2).

Design constraints (see README.md / ARCHITECTURE.md):
- the privileged Message Content intent stays OFF; Discord only exposes message
  text for messages that mention the bot (plus DMs and the bot's own messages),
  so an explicit mention is the reliable trigger;
- replies to the bot's own messages are recognized through the message reference
  when Discord resolves it, but their text is only readable when the reply also
  mentions the bot -> an honest, rate-limited hint covers the rest;
- context is bounded and durable: turns live in SQLite scoped per
  (guild, channel, user) (``core.chat_memory.ChatMemory``), with the bounded
  in-memory store as a fallback for database outages; ``/clear-conversation``
  deletes the caller's own stored turns in one scope;
- chat inference is serialized per bot (semaphore), bounded (at most
  ``CONVERSATION_MAX_WAITING`` requests may wait for the slot; the rest get one
  rate-limited notice), and guarded per scope (in-flight) with a short per-user
  cooldown; slash-command jobs are unaffected (a job can share the model with
  one chat reply, bounded by ``OLLAMA_MAX_CONCURRENT``);
- diagnostics: every stage logs a redacted, reasoned ``chat:`` line, so a smoke
  test can never fail silently again (see ``data/logs/copilot.log``);
- grounded replies (design D22): one bounded, fenced ``<kb>`` block built from
  moderator-approved knowledge-base documents (repository FTS search, at most
  ``CONVERSATION_GROUNDING_TOP_K``), dropped first when the prompt budget is
  tight; retrieval failures fail open to ordinary chat, and only documents that
  actually reached the prompt may be named as sources.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..core.attachments import AttachmentService, join_context_blocks
from ..core.chat_memory import ChatMemory, ClearResult
from ..core.conversation import (
    ConversationStore,
    Decision,
    bot_mentioned_in,
    build_chat_messages,
    build_grounded_context,
    decide,
    eligible_grounding_docs,
    grounding_source_urls,
    split_message,
)
from ..errors import (
    ApprovalSelfApprovalError,
    CopilotError,
    DatabaseError,
    NotAllowedError,
    ReplyApprovalConflictError,
    ReplyApprovalNotFoundError,
    new_error_code,
)
from ..llm.persona import (
    BUSY_REPLY,
    EMPTY_REPLY,
    HELP_REPLY,
    PERSONA_VERSION,
    REPLY_HINT,
    SYSTEM_PERSONA,
    THROTTLED_REPLY,
)
from ..ui import checks
from ..ui.embeds import build_memory_status_embed
from ..util.logging import mask_id, redact
from ..util.text import neutralize_mentions
from .base import CopilotCog

if TYPE_CHECKING:
    from ..bot import CommunityCopilot

log = logging.getLogger(__name__)

_MAX_SEEN_MESSAGE_IDS = 512
_MAX_TRACKED_USERS = 500
# Static guidance (reply hints, throttle/capacity notices) is capped at one
# message per user per window, so repeated events never produce repeated noise.
_HINT_COOLDOWN_S = 300.0
_ALLOWED_MESSAGE_TYPES = {0, 19}  # default, reply

_REPLY_STATE_CHOICES = [
    app_commands.Choice(name="Pending", value="pending"),
    app_commands.Choice(name="Approved (sending or sent state reset)", value="approved"),
    app_commands.Choice(name="Rejected", value="rejected"),
    app_commands.Choice(name="Sent", value="sent"),
    app_commands.Choice(name="Failed", value="failed"),
    app_commands.Choice(name="Expired", value="expired"),
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def reply_approval_embed(row) -> discord.Embed:
    reply_id = int(row["id"])
    state = str(row["state"])
    color = {
        "pending": discord.Color.gold(),
        "approved": discord.Color.orange(),
        "sent": discord.Color.green(),
        "rejected": discord.Color.red(),
        "failed": discord.Color.red(),
        "expired": discord.Color.dark_grey(),
    }.get(state, discord.Color.blurple())
    embed = discord.Embed(
        title=f"Held reply #{reply_id} · {state}",
        description=_clip(str(row["content"]), 4096),
        color=color,
    )
    embed.add_field(name="Channel", value=f"<#{int(row['channel_id'])}>", inline=True)
    embed.add_field(name="Requested by", value=f"<@{int(row['requested_by'])}>", inline=True)
    embed.add_field(name="Created", value=str(row["created_at"]), inline=True)
    embed.add_field(name="Expires", value=str(row["expires_at"]), inline=True)
    if row["reply_to_message_id"]:
        link = (
            f"https://discord.com/channels/{int(row['guild_id'])}/"
            f"{int(row['channel_id'])}/{int(row['reply_to_message_id'])}"
        )
        embed.add_field(name="Replying to", value=f"[original message]({link})", inline=False)
    if row["decided_by"]:
        embed.add_field(name="Decided by", value=f"<@{int(row['decided_by'])}>", inline=True)
    if row["decision_note"]:
        embed.add_field(name="Decision note", value=_clip(str(row["decision_note"]), 1024), inline=False)
    if row["sent_message_id"]:
        embed.add_field(name="Sent message id", value=f"`{row['sent_message_id']}`", inline=True)
    if row["error_code"]:
        embed.add_field(name="Error code", value=f"`{row['error_code']}`", inline=True)
    embed.set_footer(text=f"Exact stored text · hash {str(row['content_hash'])[:12]} · no edit path")
    return embed


class ReplyApprovalNoteModal(discord.ui.Modal, title="Review held reply"):
    def __init__(self, cog: ConversationCog, reply_id: int, decision: str) -> None:
        super().__init__()
        self.cog = cog
        self.reply_id = reply_id
        self.decision = decision
        self.note = discord.ui.TextInput(
            label="Reviewer note (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._decide_reply_interaction(
            interaction,
            self.reply_id,
            self.decision,
            str(self.note.value or "") or None,
        )


class ReplyApprovalView(discord.ui.View):
    """Moderator controls for one exact stored reply; content cannot be edited."""

    def __init__(self, cog: ConversationCog, reply_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.reply_id = reply_id

    @discord.ui.button(label="Approve & send", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ReplyApprovalNoteModal(self.cog, self.reply_id, "approve")
        )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ReplyApprovalNoteModal(self.cog, self.reply_id, "reject")
        )


class ConversationCog(CopilotCog):
    def __init__(self, bot: CommunityCopilot) -> None:
        super().__init__(bot)
        settings = self.settings
        self.store = ConversationStore(
            max_turns=settings.conversation_max_turns,
            max_chars=settings.conversation_max_chars,
            ttl_s=float(settings.conversation_ttl_s),
        )
        self.memory = ChatMemory(
            repo=getattr(bot, "repo", None),
            fallback=self.store,
            max_turns=settings.conversation_max_turns,
            max_stored_turns=settings.conversation_max_stored_turns,
        )
        self._inference_gate = asyncio.Semaphore(1)
        self.attachments = AttachmentService(getattr(bot, "repo", None), settings)
        self._waiting = 0  # chat requests currently waiting for the inference slot
        self._active_scopes: set[tuple[int | None, int, int]] = set()
        self._seen_message_ids: OrderedDict[int, None] = OrderedDict()
        self._last_reply_at: OrderedDict[int, float] = OrderedDict()
        self._last_hint_at: OrderedDict[int, float] = OrderedDict()
        self._saw_message_event = False
        # Injectable clock: cooldown boundaries are tested without sleeping.
        self._now: Callable[[], float] = time.monotonic

    # --------------------------------------------------------------- lifecycle

    async def cog_load(self) -> None:
        """One truthful startup line so a silent smoke test is self-explanatory."""
        settings = self.settings
        if not settings.conversation_enabled:
            log.warning(
                "chat: disabled in configuration (CONVERSATION_ENABLED=false) — "
                "mentions and replies are ignored"
            )
            return
        channels = (
            f"{len(settings.channel_allowlist)} channel(s)"
            if settings.channel_allowlist
            else "all channels of allowed guilds"
        )
        memory = (
            f"sqlite (retention={settings.conversation_retention_days}d)"
            if self.memory.persistent
            else "in-memory only (no database wired up)"
        )
        log.info(
            "chat: ready (trigger=mention or reply-to-bot, guilds=%d, channels=%s, cooldown=%ss, "
            "max_turns=%d, max_waiting=%d, memory=%s, message content intent=off)",
            len(settings.guild_allowlist),
            channels,
            settings.conversation_cooldown_s,
            settings.conversation_max_turns,
            settings.conversation_max_waiting,
            memory,
        )

    # ---------------------------------------------------------------- listener

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._handle(message)
        except Exception:  # noqa: BLE001 - a chat handler must never kill the event loop
            log.exception("Conversation handler crashed")

    async def _handle(self, message) -> bool:
        """Handle one message.

        Returns True only when a reply (including a throttled/capacity notice)
        was actually DELIVERED to Discord; delivery failures are logged with
        ``delivered=False`` and reported as False here, never claimed as sent.
        """
        settings = self.settings
        if not settings.conversation_enabled:
            return False
        if not self._saw_message_event:
            self._saw_message_event = True
            log.info("chat: message events are flowing (first event of this run)")

        author = getattr(message, "author", None)
        log.debug(
            "chat: event id=%s guild=%s channel=%s author_bot=%s webhook=%s type=%s mentions=%d chars=%d",
            mask_id(getattr(message, "id", None)),
            mask_id(getattr(getattr(message, "guild", None), "id", None)),
            mask_id(getattr(getattr(message, "channel", None), "id", None)),
            bool(getattr(author, "bot", False)),
            getattr(message, "webhook_id", None) is not None,
            getattr(message, "type", None),
            len(getattr(message, "mentions", ()) or ()),
            len(getattr(message, "content", "") or ""),
        )
        if getattr(author, "bot", False):
            return False  # covers ourselves and other bots: no loops
        if getattr(message, "guild", None) is None:
            return False  # DMs are not supported anywhere in this bot
        if getattr(message, "webhook_id", None) is not None:
            return False  # webhook messages are not a trusted trigger source

        bot_id = int(getattr(self.bot.user, "id", 0) or 0)
        if not bot_id:
            return False
        user_id = int(getattr(author, "id", 0) or 0)
        mentioned = bot_mentioned_in(getattr(message, "mentions", ()) or (), bot_id)
        reply_to_bot = self._reply_targets_bot(message, bot_id)
        trigger = mentioned or reply_to_bot
        trigger_kind = "mention" if mentioned else "reply-to-bot"
        if trigger:
            log.info(
                "chat: trigger detected (reason=%s user=%s guild=%s channel=%s)",
                trigger_kind,
                mask_id(user_id),
                mask_id(getattr(getattr(message, "guild", None), "id", None)),
                mask_id(getattr(getattr(message, "channel", None), "id", None)),
            )

        if not self._is_allowed(message):
            if trigger:
                channel = getattr(message, "channel", None)
                log.warning(
                    "chat: trigger dropped — outside the configured allowlists "
                    "(guild=%s channel=%s parent=%s)",
                    mask_id(getattr(getattr(message, "guild", None), "id", None)),
                    mask_id(getattr(channel, "id", None)),
                    mask_id(getattr(channel, "parent_id", None)),
                )
            return False  # same allowlists as slash commands
        if not self._message_type_ok(message):
            if trigger:
                log.warning(
                    "chat: trigger dropped — unsupported message type (%s)",
                    getattr(getattr(message, "type", None), "value", getattr(message, "type", None)),
                )
            return False
        if not self._first_time_seen(getattr(message, "id", None)):
            log.debug(
                "chat: duplicate gateway delivery ignored (id=%s)",
                mask_id(getattr(message, "id", None)),
            )
            return False

        decision, question = decide(
            author_is_bot=bool(getattr(author, "bot", False)),
            mentioned_bot=mentioned,
            reply_to_bot=reply_to_bot,
            content=getattr(message, "content", "") or "",
            bot_id=bot_id,
        )

        if decision is Decision.IGNORE:
            log.debug("chat: ignored (no trigger, id=%s)", mask_id(getattr(message, "id", None)))
            return False
        if decision is Decision.HELP:
            if not self._rate_gate(self._last_hint_at, user_id, _HINT_COOLDOWN_S):
                return False
            delivered = await self._send_reply(message, HELP_REPLY)
            log.info("chat: help sent (user=%s delivered=%s)", mask_id(user_id), delivered)
            return delivered
        if decision is Decision.HINT:
            if not self._rate_gate(self._last_hint_at, user_id, _HINT_COOLDOWN_S):
                return False
            delivered = await self._send_reply(message, REPLY_HINT)
            log.info("chat: hint sent (user=%s delivered=%s)", mask_id(user_id), delivered)
            return delivered

        # Decision.RESPOND: run one bounded, serialized model call.
        scope = self._scope_key(message)
        if scope in self._active_scopes:
            # The user can see the typing indicator of the in-flight reply, so a
            # second notice here would only be noise.
            log.debug("chat: response already in flight for this scope; skipping")
            return False
        if not self._rate_gate(self._last_reply_at, user_id, settings.conversation_cooldown_s):
            log.debug("chat: cooldown active (user=%s); skipping", mask_id(user_id))
            return await self._static_notice(message, user_id, THROTTLED_REPLY, reason="cooldown")

        self._active_scopes.add(scope)
        try:
            async with message.channel.typing():
                images, image_note, attachment_block = await self._attachment_inputs(message)
                history = await self.memory.history(scope)
                context_block, grounded_docs = await self._retrieve_grounding(
                    question, guild_id=scope[0]
                )
                context_block = join_context_blocks(attachment_block, context_block)
                payload = build_chat_messages(
                    SYSTEM_PERSONA,
                    history,
                    question,
                    max_chars=settings.conversation_max_chars,
                    context_block=context_block,
                )
                if context_block and not self._block_reached_prompt(
                    payload, context_block, question
                ):
                    # The builder dropped the block to fit the budget: the reply is
                    # no longer grounded, so neither the footer nor the log may say so.
                    grounded_docs = []
                    if attachment_block:
                        dropped_note = (
                            "Note: attached file text was too large to include in this request."
                        )
                        image_note = f"{image_note}\n{dropped_note}" if image_note else dropped_note
                if not await self._acquire_inference_slot():
                    return await self._static_notice(message, user_id, BUSY_REPLY, reason="capacity")
                log.debug(
                    "chat: model request started (user=%s history=%d images=%d)",
                    mask_id(user_id),
                    len(history),
                    len(images),
                )
                try:
                    reply_text = await self.provider.chat_messages(
                        payload,
                        images=images,
                        timeout_s=settings.conversation_timeout_s,
                        max_tokens=settings.conversation_max_tokens,
                    )
                finally:
                    self._inference_gate.release()
        except CopilotError as exc:
            delivered = await self._send_reply(message, exc.user_message())
            log.info(
                "chat: model error (user=%s type=%s delivered=%s): %s",
                mask_id(user_id),
                type(exc).__name__,
                delivered,
                exc,
            )
            return delivered
        except Exception:  # noqa: BLE001 - never leak a traceback to Discord
            code = new_error_code()
            log.exception("chat: unexpected error (%s)", code)
            delivered = await self._send_reply(message, f"Unexpected error. `{code}`")
            return delivered
        finally:
            self._active_scopes.discard(scope)

        if not reply_text or not reply_text.strip():
            log.warning("chat: empty model output (user=%s)", mask_id(user_id))
            delivered = await self._send_reply(message, EMPTY_REPLY)
            return delivered

        cleaned = reply_text.strip()
        display = neutralize_mentions(cleaned)
        if image_note:
            display = f"{display}\n\n{image_note}"
        footer = self._grounding_footer(grounded_docs)
        if footer:
            display = f"{display}\n\n{footer}"
        if settings.reply_approval_enabled:
            # Public model-generated replies are gated when the operator enables
            # reply approval. Static help/hint notices above stay unaffected: they
            # are deterministic text with no model output or community data.
            queued = await self._queue_reply_approval(message, display, trigger=trigger_kind)
            log.info(
                "chat: reply held for human approval (trigger=%s user=%s queued=%s chars=%d)",
                trigger_kind,
                mask_id(user_id),
                queued,
                len(display),
            )
            return False
        chunks = split_message(display)
        # One attempt per chunk, in order; the first failure stops delivery so a
        # partially delivered reply can never grow duplicates or arrive mangled.
        delivered = await self._send_reply(message, chunks[0], part=1, total=len(chunks))
        sent = 1 if delivered else 0
        if delivered:
            for part, chunk in enumerate(chunks[1:], start=2):
                if not await self._send_chunk(message, chunk, part=part, total=len(chunks)):
                    break
                sent = part
        delivered = sent == len(chunks)
        # Only remember an answer the user actually received. A held or failed
        # reply must not appear in the next prompt as if it had been sent.
        persisted = await self.memory.append(scope, question, cleaned) if delivered else False
        log.info(
            "chat: replied (persona=%s user=%s chars=%d chunks=%d sent=%d history=%d images=%d "
            "grounded=%d delivered=%s persisted=%s)",
            PERSONA_VERSION,
            mask_id(user_id),
            len(display),
            len(chunks),
            sent,
            len(history),
            len(images),
            len(grounded_docs),
            delivered,
            persisted,
        )
        return delivered

    # --------------------------------------------------------------- commands

    @app_commands.command(
        name="conversation-status",
        description="Show what conversation memory the bot stores for YOU (counts only)",
    )
    @app_commands.describe(channel="Channel or thread to inspect; defaults to this channel")
    async def conversation_status(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not self.settings.conversation_enabled:
            await self.reply(
                interaction,
                content="Conversation features are disabled on this bot, so no chat memory is stored.",
            )
            return

        guild_id, current_channel_id, _parent_id = checks.interaction_ids(interaction)
        target_channel_id = current_channel_id
        if channel is not None:
            target_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if target_guild_id is not None and target_guild_id != guild_id:
                await self.reply(
                    interaction,
                    content="You can only inspect your own memory in this server.",
                )
                return
            if not checks.channel_allowed(
                self.settings.channel_allowlist,
                getattr(channel, "id", None),
                getattr(channel, "parent_id", None),
            ):
                await self.reply(interaction, content=str(NotAllowedError()))
                return
            target_channel_id = getattr(channel, "id", None)

        if guild_id is None or target_channel_id is None:
            await self.reply(interaction, content=str(NotAllowedError()))
            return

        await interaction.response.defer(ephemeral=True)
        stats = await self.memory.stats((guild_id, int(target_channel_id), int(interaction.user.id)))
        await self.reply(
            interaction,
            embed=build_memory_status_embed(
                channel_id=int(target_channel_id),
                stats=stats,
                retention_days=self.settings.conversation_retention_days,
                max_stored_turns=self.settings.conversation_max_stored_turns,
                context_turns=self.settings.conversation_max_turns,
                grounding_enabled=self.settings.conversation_grounding_enabled,
            ),
        )

    @app_commands.command(
        name="clear-conversation",
        description="Delete YOUR stored conversation history with the bot (current channel by default)",
    )
    @app_commands.describe(channel="Channel or thread to clear; defaults to this channel")
    async def clear_conversation(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not self.settings.conversation_enabled:
            await self.reply(
                interaction,
                content=(
                    "Conversation features are disabled on this bot, "
                    "so there is no chat history to clear."
                ),
            )
            return

        guild_id, current_channel_id, _parent_id = checks.interaction_ids(interaction)
        target_channel_id = current_channel_id
        if channel is not None:
            target_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if target_guild_id is not None and target_guild_id != guild_id:
                await self.reply(
                    interaction,
                    content="You can only clear your own history in this server.",
                )
                return
            if not checks.channel_allowed(
                self.settings.channel_allowlist,
                getattr(channel, "id", None),
                getattr(channel, "parent_id", None),
            ):
                await self.reply(interaction, content=str(NotAllowedError()))
                return
            target_channel_id = getattr(channel, "id", None)

        if guild_id is None or target_channel_id is None:
            await self.reply(interaction, content=str(NotAllowedError()))
            return

        user_id = int(interaction.user.id)
        scope = (guild_id, int(target_channel_id), user_id)
        if scope in self._active_scopes:
            # Storing the in-flight reply right after a clear would undo it.
            await self.reply(
                interaction,
                content=(
                    "A reply to you is still being generated in that channel — "
                    "run this command again in a few seconds."
                ),
            )
            return

        # Ack before the database work (same convention as the moderator commands).
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.memory.clear(scope)
        except DatabaseError as exc:
            log.warning("chat: clear-conversation failed: %s", redact(str(exc))[:200])
            await self.reply(
                interaction,
                content=(
                    "Could not clear stored history — the local database is unavailable. "
                    "Nothing was deleted; try again shortly."
                ),
            )
            return

        attachment_count = await self.attachments.clear_scope(
            guild_id=guild_id, channel_id=int(target_channel_id), user_id=user_id
        )
        if result.deleted or result.memory_existed or attachment_count:
            await self._audit_clear(
                interaction, int(target_channel_id), result, attachment_count
            )
        log.info(
            "chat: history cleared (user=%s channel=%s db_turns=%d attachments=%d memory_existed=%s)",
            mask_id(user_id),
            mask_id(target_channel_id),
            result.deleted,
            attachment_count,
            result.memory_existed,
        )
        await self.reply(
            interaction,
            content=self._clear_confirmation(
                int(target_channel_id), result, attachment_count
            ),
        )

    @staticmethod
    def _clear_confirmation(
        channel_id: int, result: ClearResult, attachment_count: int = 0
    ) -> str:
        mention = f"<#{channel_id}>"
        attachment_note = (
            f" Also deleted metadata/extracted text for {attachment_count} attachment(s)."
            if attachment_count
            else ""
        )
        if result.deleted:
            return (
                f"Cleared **{result.deleted}** stored conversation turn(s) of your history in "
                f"{mention}. Only your own chat history was removed — summaries, solutions, "
                f"and the knowledge base are untouched.{attachment_note}"
            )
        if result.memory_existed or attachment_count:
            return (
                f"Cleared your in-process conversation context for {mention} "
                f"(nothing had been stored in the database).{attachment_note}"
            )
        return f"You have no stored conversation history in {mention} — nothing to clear."

    async def _audit_clear(
        self,
        interaction: discord.Interaction,
        channel_id: int,
        result: ClearResult,
        attachment_count: int = 0,
    ) -> None:
        repo = getattr(self.bot, "repo", None)
        if repo is None:
            return
        try:
            await repo.audit(
                actor_id=str(interaction.user.id),
                action="conversation.cleared",
                target_type="channel",
                target_id=str(channel_id),
                guild_id=str(interaction.guild_id),
                meta={
                    "guild_id": str(interaction.guild_id),
                    "turns": result.deleted,
                    "attachments": attachment_count,
                    "memory_only": not result.persistent,
                },
            )
        except DatabaseError:  # an audit failure must not mask a successful clear
            log.warning("chat: could not write the conversation-clear audit row")

    # --------------------------------------------------------- reply approvals

    @app_commands.command(
        name="reply-approvals",
        description="Moderator: review public bot replies held for explicit approval",
    )
    @app_commands.choices(state=_REPLY_STATE_CHOICES)
    async def reply_approvals(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str] | None = None,
    ) -> None:
        if not self.settings.reply_approval_enabled:
            await self.reply(
                interaction,
                content=(
                    "Reply approval is disabled on this bot. Model-generated mention replies "
                    "are sent directly; enable `REPLY_APPROVAL_ENABLED` to hold them for review."
                ),
            )
            return
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            await self.reply(interaction, content=str(NotAllowedError()))
            return
        wanted = state.value if state is not None else "pending"
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.repo.expire_reply_approvals(now=_now_iso())
            rows = await self.repo.list_reply_approvals(
                guild_id=str(guild_id), state=wanted, limit=5
            )
            if not rows:
                await self.reply(interaction, content=f"No **{wanted}** reply approvals in this server.")
                return
            for row in rows:
                view = ReplyApprovalView(self, int(row["id"])) if wanted == "pending" else None
                await self.reply(interaction, embed=reply_approval_embed(row), view=view)
        except Exception as exc:  # noqa: BLE001
            await self.handle_error(interaction, exc, action="listing held replies")

    @app_commands.command(
        name="reply-approve",
        description="Moderator: approve and send one exact held reply to its stored channel",
    )
    async def reply_approve(
        self,
        interaction: discord.Interaction,
        reply_id: int,
        note: str | None = None,
    ) -> None:
        await self._decide_reply_interaction(interaction, reply_id, "approve", note)

    @app_commands.command(
        name="reply-reject",
        description="Moderator: reject one held reply; nothing is sent",
    )
    async def reply_reject(
        self,
        interaction: discord.Interaction,
        reply_id: int,
        note: str | None = None,
    ) -> None:
        await self._decide_reply_interaction(interaction, reply_id, "reject", note)

    async def _queue_reply_approval(self, message, content: str, *, trigger: str) -> bool:
        """Store one exact public reply for human review; never falls back to sending."""
        repo = getattr(self.bot, "repo", None)
        if repo is None:
            log.error("chat: reply approval is enabled but no repository is wired; reply was not sent")
            return False
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        user_id = getattr(getattr(message, "author", None), "id", None)
        message_id = getattr(message, "id", None)
        if guild_id is None or channel_id is None or user_id is None:
            return False
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=int(self.settings.reply_approval_ttl_s))
        try:
            reply_id = await repo.insert_reply_approval(
                guild_id=str(guild_id),
                channel_id=str(channel_id),
                reply_to_message_id=None if message_id is None else str(message_id),
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                requested_by=str(user_id),
                created_at=now.isoformat(timespec="seconds"),
                expires_at=expires.isoformat(timespec="seconds"),
            )
            action = (
                "chat.mention_reply_approval_requested"
                if trigger == "mention"
                else "chat.chat_reply_approval_requested"
            )
            await repo.audit(
                actor_id=str(user_id),
                action=action,
                target_type="reply_approval",
                target_id=str(reply_id),
                guild_id=str(guild_id),
                meta={
                    "guild_id": str(guild_id),
                    "channel_id": str(channel_id),
                    "trigger": trigger,
                    "chars": len(content),
                    "path": "mention_reply" if trigger == "mention" else "chat_reply",
                },
            )
        except Exception as exc:  # noqa: BLE001 - never send unapproved content
            log.warning(
                "chat: could not queue reply approval (%s); reply was not sent",
                type(exc).__name__,
            )
            return False
        log.info(
            "chat: reply approval queued (id=%s trigger=%s channel=%s)",
            reply_id,
            trigger,
            mask_id(channel_id),
        )
        return True

    async def _decide_reply_interaction(
        self,
        interaction: discord.Interaction,
        reply_id: int,
        decision: str,
        note: str | None,
    ) -> None:
        if not self.settings.reply_approval_enabled:
            await self.reply(interaction, content="Reply approval is disabled on this bot.")
            return
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        if not await checks.ensure_moderator(interaction, self.settings):
            return
        guild_id = getattr(interaction, "guild_id", None)
        reviewer_id = str(getattr(getattr(interaction, "user", None), "id", ""))
        if guild_id is None:
            await self.reply(interaction, content=str(NotAllowedError()))
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            row, sent_chunks, total_chunks = await self._review_reply_approval(
                guild_id=int(guild_id),
                reviewer_id=reviewer_id,
                reply_id=reply_id,
                decision=decision,
                note=note,
            )
            if decision == "reject":
                await self.reply(
                    interaction,
                    content=f"Reply approval `#{reply_id}` rejected and discarded. Nothing was sent.",
                )
                return
            if sent_chunks == total_chunks:
                await self.reply(
                    interaction,
                    embed=reply_approval_embed(row),
                    content=f"Approved and sent {sent_chunks} chunk(s) to the stored channel.",
                )
            elif sent_chunks:
                await self.reply(
                    interaction,
                    embed=reply_approval_embed(row),
                    content=(
                        f"Approval recorded, but only {sent_chunks}/{total_chunks} chunk(s) were "
                        "delivered. Do not retry: the remaining text is not re-sent automatically."
                    ),
                )
            else:
                await self.reply(
                    interaction,
                    embed=reply_approval_embed(row),
                    content=(
                        "Approval recorded, but delivery failed. The exact reply was not sent; "
                        "nothing is retried automatically."
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - interaction callback must reply safely
            await self.handle_error(interaction, exc, action="reviewing the held reply")

    async def _review_reply_approval(
        self,
        *,
        guild_id: int,
        reviewer_id: str,
        reply_id: int,
        decision: str,
        note: str | None,
    ):
        if decision not in {"approve", "reject"}:
            raise ReplyApprovalConflictError("Unknown reply approval decision.")
        repo = self.repo
        await repo.expire_reply_approvals(now=_now_iso())
        row = await repo.get_reply_approval(reply_id, guild_id=str(guild_id))
        if row is None:
            raise ReplyApprovalNotFoundError()
        if str(row["state"]) != "pending":
            raise ReplyApprovalConflictError()
        if str(row["requested_by"]) == reviewer_id:
            await self._audit_reply_denial(guild_id, reply_id, reviewer_id, "self_approval")
            raise ApprovalSelfApprovalError()
        expected = hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
        if expected != str(row["content_hash"]):
            await self._audit_reply_denial(guild_id, reply_id, reviewer_id, "hash_mismatch")
            raise ReplyApprovalConflictError(
                "The stored reply no longer matches its approval hash; nothing was sent."
            )
        state = "approved" if decision == "approve" else "rejected"
        decided = await repo.decide_reply_approval(
            reply_id,
            guild_id=str(guild_id),
            state=state,
            decided_by=reviewer_id,
            decided_at=_now_iso(),
            note=note,
            audit_meta={
                "guild_id": str(guild_id),
                "channel_id": str(row["channel_id"]),
                "requested_by": str(row["requested_by"]),
                "decision": state,
                "chars": len(str(row["content"])),
            },
        )
        if not decided:
            raise ReplyApprovalConflictError()
        refreshed = await repo.get_reply_approval(reply_id, guild_id=str(guild_id))
        if decision == "reject":
            return refreshed or row, 0, 0
        return await self._deliver_reply_approval(refreshed or row, guild_id=guild_id, approver_id=reviewer_id)

    async def _deliver_reply_approval(self, row, *, guild_id: int, approver_id: str):
        """Send the exact stored text to the exact stored channel. Never re-edits."""
        content = str(row["content"])
        chunks = split_message(content)
        repo = self.repo
        if str(row["expires_at"]) <= _now_iso():
            await self._finish_reply_approval_failure(row, guild_id, "approval_expired")
            refreshed = await repo.get_reply_approval(int(row["id"]), guild_id=str(guild_id))
            return refreshed or row, 0, len(chunks)
        channel = None
        failure = "destination_unavailable"
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            channel = guild.get_channel(int(row["channel_id"]))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(row["channel_id"]))
                except Exception as exc:  # noqa: BLE001 - unavailable channel is a typed failure
                    log.warning(
                        "chat: held reply channel unavailable (%s)", type(exc).__name__
                    )
            if channel is not None and int(
                getattr(getattr(channel, "guild", None), "id", 0) or 0
            ) != guild_id:
                channel = None
                failure = "cross_guild_destination"
            if channel is not None and not checks.channel_allowed(
                self.settings.channel_allowlist,
                getattr(channel, "id", None),
                getattr(channel, "parent_id", None),
            ):
                channel = None
                failure = "channel_not_allowed"
        if channel is None:
            await self._finish_reply_approval_failure(row, guild_id, failure)
            return await repo.get_reply_approval(int(row["id"]), guild_id=str(guild_id)) or row, 0, len(chunks)

        reference = None
        if row["reply_to_message_id"]:
            try:
                reference = channel.get_partial_message(int(row["reply_to_message_id"]))
            except Exception:  # noqa: BLE001 - fall back to a plain channel send
                reference = None
        sent_ids: list[str] = []
        for index, chunk in enumerate(chunks):
            try:
                if index == 0 and reference is not None:
                    sent = await reference.reply(
                        chunk,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    sent = await channel.send(
                        chunk, allowed_mentions=discord.AllowedMentions.none()
                    )
            except discord.NotFound:
                if index != 0 or reference is None:
                    log.warning("chat: held reply delivery failed (NotFound)")
                    break
                try:
                    sent = await channel.send(
                        chunk, allowed_mentions=discord.AllowedMentions.none()
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "chat: held reply delivery failed (%s)", type(exc).__name__
                    )
                    break
            except Exception as exc:  # noqa: BLE001
                log.warning("chat: held reply delivery failed (%s)", type(exc).__name__)
                break
            sent_ids.append(str(sent.id))

        if sent_ids:
            try:
                await repo.mark_reply_approval_sent(
                    int(row["id"]),
                    guild_id=str(guild_id),
                    message_id=sent_ids[0],
                    now=_now_iso(),
                )
            except Exception as exc:  # noqa: BLE001 - message is already public
                log.warning(
                    "chat: could not record held reply delivery (%s)", type(exc).__name__
                )
            await repo.audit(
                actor_id=approver_id,
                action="chat.reply_approval_sent",
                target_type="reply_approval",
                target_id=str(row["id"]),
                guild_id=str(guild_id),
                meta={
                    "guild_id": str(guild_id),
                    "chunks_sent": len(sent_ids),
                    "chunks_total": len(chunks),
                    "message_id": sent_ids[0],
                },
            )
        else:
            await self._finish_reply_approval_failure(row, guild_id, "delivery_failed")
        refreshed = await repo.get_reply_approval(int(row["id"]), guild_id=str(guild_id))
        return refreshed or row, len(sent_ids), len(chunks)

    async def _finish_reply_approval_failure(self, row, guild_id: int, error_code: str) -> None:
        try:
            await self.repo.mark_reply_approval_failed(
                int(row["id"]),
                guild_id=str(guild_id),
                error_code=error_code,
                now=_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 - best effort record; nothing was sent
            log.warning(
                "chat: could not record held reply failure (%s)", type(exc).__name__
            )
        try:
            await self.repo.audit(
                actor_id="system",
                action="chat.reply_approval_send_failed",
                target_type="reply_approval",
                target_id=str(row["id"]),
                guild_id=str(guild_id),
                meta={"guild_id": str(guild_id), "error_code": error_code},
            )
        except Exception:  # noqa: BLE001
            log.warning("chat: could not audit held reply failure")

    async def _audit_reply_denial(
        self, guild_id: int, reply_id: int, reviewer_id: str, reason: str
    ) -> None:
        try:
            await self.repo.audit(
                actor_id=reviewer_id,
                action="chat.reply_approval_denied",
                target_type="reply_approval",
                target_id=str(reply_id),
                guild_id=str(guild_id),
                meta={"guild_id": str(guild_id), "reason": reason},
            )
        except Exception:  # noqa: BLE001 - denial still raises to the caller
            log.warning("chat: could not audit held reply denial")

    # ----------------------------------------------------------------- filters

    def _is_allowed(self, message) -> bool:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        if not checks.guild_allowed(self.settings.guild_allowlist, guild_id):
            return False
        channel = getattr(message, "channel", None)
        return checks.channel_allowed(
            self.settings.channel_allowlist,
            getattr(channel, "id", None),
            getattr(channel, "parent_id", None),
        )

    @staticmethod
    def _message_type_ok(message) -> bool:
        message_type = getattr(message, "type", 0)
        try:
            value = int(getattr(message_type, "value", message_type) or 0)
        except (TypeError, ValueError):
            value = 0
        return value in _ALLOWED_MESSAGE_TYPES

    def _first_time_seen(self, message_id) -> bool:
        if message_id is None:
            return True
        try:
            key = int(message_id)
        except (TypeError, ValueError):
            return True
        if key in self._seen_message_ids:
            return False
        self._seen_message_ids[key] = None
        while len(self._seen_message_ids) > _MAX_SEEN_MESSAGE_IDS:
            self._seen_message_ids.popitem(last=False)
        return True

    @staticmethod
    def _reply_targets_bot(message, bot_id: int) -> bool:
        """True when the message is a reply to one of our messages (resolved by Discord)."""
        reference = getattr(message, "reference", None)
        if reference is None:
            return False
        resolved = getattr(reference, "resolved", None)
        author = getattr(resolved, "author", None)
        return getattr(author, "id", None) == bot_id

    @staticmethod
    def _scope_key(message) -> tuple[int | None, int, int]:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
        user_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)
        return (guild_id, channel_id, user_id)

    def _rate_gate(self, table: OrderedDict, user_id: int, cooldown_s: float) -> bool:
        """True when this class of reply is allowed now; records the attempt.

        Uses the injectable ``self._now`` clock, so cooldown boundaries are
        tested deterministically without sleeping. Tables stay bounded by
        evicting the oldest user.
        """
        now = self._now()
        last = table.get(user_id)
        if last is not None and now - last < cooldown_s:
            return False
        table[user_id] = now
        table.move_to_end(user_id)
        while len(table) > _MAX_TRACKED_USERS:
            table.popitem(last=False)
        return True

    # ------------------------------------------------------- admission/notices

    async def _acquire_inference_slot(self) -> bool:
        """Take the bot-wide chat slot, honouring ``CONVERSATION_MAX_WAITING``.

        At most one generation runs and at most the configured number of chat
        requests may WAIT for it; anything beyond that returns False so the
        caller can answer with one rate-limited notice instead of piling up
        waiting tasks (each of which would hold a live typing indicator).
        The ``locked()`` check and the ``acquire()`` happen without an await in
        between, so the waiter count cannot be raced past its bound.
        """
        if self._inference_gate.locked():
            if self._waiting >= self.settings.conversation_max_waiting:
                log.debug(
                    "chat: at capacity (waiting=%d limit=%d)",
                    self._waiting,
                    self.settings.conversation_max_waiting,
                )
                return False
            self._waiting += 1
            try:
                await self._inference_gate.acquire()
            finally:
                self._waiting -= 1
        else:
            await self._inference_gate.acquire()
        return True

    async def _static_notice(self, message, user_id: int, text: str, *, reason: str) -> bool:
        """Send one model-free notice, capped at one per user per hint window.

        Used for throttled and at-capacity requests: the user gets actionable
        feedback once, and repeated events stay silent instead of noisy.
        """
        if not self._rate_gate(self._last_hint_at, user_id, _HINT_COOLDOWN_S):
            log.debug(
                "chat: notice suppressed (user=%s reason=%s)", mask_id(user_id), reason
            )
            return False
        delivered = await self._send_reply(message, text)
        log.info(
            "chat: notice sent (user=%s reason=%s delivered=%s)", mask_id(user_id), reason, delivered
        )
        return delivered

    # ---------------------------------------------------------------- grounding

    async def _retrieve_grounding(
        self, question: str, *, guild_id: int | None
    ) -> tuple[str | None, list]:
        """Approved, same-guild knowledge-base context; never raises.

        Runs only when grounding is enabled, a repository is wired up, and the
        message has a guild. It asks the repository's own FTS/BM25 search
        (``search_kb``) for at most ``CONVERSATION_GROUNDING_TOP_K`` documents,
        scoped to this guild and to ``approved`` rows only. Auto-indexed thread
        fingerprints and pending proposals are therefore never eligible; shared
        ``kb_docs`` across guilds can never leak in. ``eligible_grounding_docs``
        re-checks the approval flag defensively. Retrieval failures fail open:
        the reply proceeds exactly as without grounding, and the user is told
        nothing about the internal error.
        """
        settings = self.settings
        if not settings.conversation_grounding_enabled or guild_id is None:
            return None, []
        repo = getattr(self.bot, "repo", None)
        if repo is None:
            return None, []  # no persistent store: no approved corpus to read
        try:
            rows = await repo.search_kb(
                question,
                guild_id=str(guild_id),
                limit=settings.conversation_grounding_top_k,
                approved_only=True,
            )
            docs = eligible_grounding_docs(rows)
            block, included = build_grounded_context(
                docs, settings.conversation_grounding_max_chars
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must never block a reply
            # Type name only: the question (user text) is part of the FTS query
            # and must never end up in a log line, even inside an error message.
            log.warning(
                "chat: KB retrieval failed (%s); answering without grounding",
                type(exc).__name__,
            )
            return None, []
        if not included:
            return None, []
        log.debug("chat: grounding attached (docs=%d chars=%d)", len(included), len(block))
        return block, included

    @staticmethod
    def _block_reached_prompt(
        payload: list[dict[str, str]], context_block: str | None, question: str
    ) -> bool:
        """True when the payload builder actually kept the block in the prompt.

        The builder drops the optional block first when the budget is tight, so
        the source footer and the ``grounded`` log field must follow the
        payload, not the retrieval.
        """
        block = (context_block or "").strip()
        if not block or not payload:
            return False
        return payload[-1]["content"] == f"{block}\n\n{question}"

    @staticmethod
    def _grounding_footer(docs: Sequence[object]) -> str:
        """One short, clearly-unverified source footer; only for included docs.

        Links are plain-text, validated http(s) URLs (at most two) taken from
        the documents that made it into the prompt; when none has a usable URL
        the footer is omitted entirely rather than approximated.
        """
        urls = grounding_source_urls(docs, limit=2)
        if not urls:
            return ""
        links = "\n".join(f"- {url}" for url in urls)
        return f"Possibly related (not verified):\n{links}"

    # ------------------------------------------------------------- attachments

    async def _attachment_inputs(self, message) -> tuple[list[bytes], str | None, str | None]:
        """Return ``(vision_images, user_note, untrusted_context_block)``.

        Documents are extracted and fenced as untrusted data; images reach the
        model only when the configured provider genuinely reports vision support.
        Earlier attachments in the same (guild, channel, user) scope are added as
        bounded follow-up context. No raw image or document bytes are persisted.
        """
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        author = getattr(message, "author", None)
        user_id = getattr(author, "id", None)
        message_id = str(getattr(message, "id", "") or "")
        has_attachments = bool(getattr(message, "attachments", ()) or ())
        if not self.settings.attachments_enabled:
            # The setting must stop old stored extracts from entering prompts too,
            # not only new ingestion.
            note = "Attachment reading is disabled in configuration." if has_attachments else None
            return [], note, None

        followup = await self.attachments.followup_block(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            exclude_message_id=message_id,
        )
        if not has_attachments:
            return [], None, followup

        vision_supported = False
        if self.settings.vision_enabled and self._has_image_hint(message):
            try:
                capabilities = await self.provider.capabilities()
                vision_supported = bool(
                    capabilities.exists and capabilities.supports_vision
                )
            except Exception:  # noqa: BLE001 - an unknown provider has no vision
                vision_supported = False

        bundle = await self.attachments.ingest_message(
            message,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            vision_supported=vision_supported,
        )
        note_text = bundle.notes_text()
        note = f"_Note: {note_text}_" if note_text else None
        return (
            bundle.images,
            note,
            join_context_blocks(bundle.context_block, followup),
        )

    @staticmethod
    def _has_image_hint(message) -> bool:
        """True when an attachment might be an image.

        Magic bytes, not the declared MIME type or filename, decide the actual
        format inside ingestion. Any attachment can therefore be an image, so
        the capability probe must not be gated on client-supplied hints.
        """
        return bool(getattr(message, "attachments", ()) or ())

    # ------------------------------------------------------------------ sending

    @staticmethod
    def _delivery_error(exc: BaseException) -> str:
        """One redacted, typed, actionable diagnostic for a failed delivery."""
        status = getattr(exc, "status", None)
        label = type(exc).__name__ if status is None else f"{type(exc).__name__} status={status}"
        return f"{label}: {redact(str(exc))[:200]}"

    async def _send_reply(self, message, content: str, *, part: int = 1, total: int = 1) -> bool:
        """Deliver one reply chunk; never raises (deleted channels, permissions,
        rate limits, HTTP failures and timeouts are all delivery failures).

        ``AllowedMentions.none()`` is part of the mass-mention policy and stays.
        """
        try:
            await message.reply(
                content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:  # noqa: BLE001 - a delivery failure must not crash the flow
            log.warning(
                "chat: reply delivery failed (part %d/%d): %s",
                part,
                total,
                self._delivery_error(exc),
            )
            return False
        return True

    async def _send_chunk(self, message, content: str, *, part: int = 1, total: int = 1) -> bool:
        """Deliver one continuation chunk; never raises (see ``_send_reply``)."""
        try:
            await message.channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:  # noqa: BLE001 - a delivery failure must not crash the flow
            log.warning(
                "chat: chunk delivery failed (part %d/%d): %s",
                part,
                total,
                self._delivery_error(exc),
            )
            return False
        return True
