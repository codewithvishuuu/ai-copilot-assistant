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
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..core.chat_memory import ChatMemory, ClearResult
from ..core.collector import CollectedAttachment
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
from ..core.images import prepare_images
from ..errors import CopilotError, DatabaseError, NotAllowedError, new_error_code
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
from ..util.logging import mask_id, redact
from ..util.text import neutralize_mentions, truncate
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
        if trigger:
            log.info(
                "chat: trigger detected (reason=%s user=%s guild=%s channel=%s)",
                "mention" if mentioned else "reply-to-bot",
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
                images, image_note = await self._prepare_attachments(message)
                history = await self.memory.history(scope)
                context_block, grounded_docs = await self._retrieve_grounding(
                    question, guild_id=scope[0]
                )
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
        persisted = await self.memory.append(scope, question, cleaned)
        display = neutralize_mentions(cleaned)
        if image_note:
            display = f"{display}\n\n{image_note}"
        footer = self._grounding_footer(grounded_docs)
        if footer:
            display = f"{display}\n\n{footer}"
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

        if result.deleted or result.memory_existed:
            await self._audit_clear(interaction, int(target_channel_id), result)
        log.info(
            "chat: history cleared (user=%s channel=%s db_turns=%d memory_existed=%s)",
            mask_id(user_id),
            mask_id(target_channel_id),
            result.deleted,
            result.memory_existed,
        )
        await self.reply(
            interaction, content=self._clear_confirmation(int(target_channel_id), result)
        )

    @staticmethod
    def _clear_confirmation(channel_id: int, result: ClearResult) -> str:
        mention = f"<#{channel_id}>"
        if result.deleted:
            return (
                f"Cleared **{result.deleted}** stored conversation turn(s) of your history in "
                f"{mention}. Only your own chat history was removed — summaries, solutions, "
                "and the knowledge base are untouched."
            )
        if result.memory_existed:
            return (
                f"Cleared your in-process conversation context for {mention} "
                "(nothing had been stored in the database)."
            )
        return f"You have no stored conversation history in {mention} — nothing to clear."

    async def _audit_clear(
        self, interaction: discord.Interaction, channel_id: int, result: ClearResult
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
                meta={"turns": result.deleted, "memory_only": not result.persistent},
            )
        except DatabaseError:  # an audit failure must not mask a successful clear
            log.warning("chat: could not write the conversation-clear audit row")

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

    async def _prepare_attachments(self, message) -> tuple[list[bytes], str | None]:
        """Run Discord attachments through the existing safe image pipeline.

        Only images the pipeline verifies (magic bytes + re-encode, bounded size)
        are handed to the model; anything skipped is surfaced in one short note.
        Live interpretation quality is NOT claimed until a live vision test passes.
        """
        attachments = list(getattr(message, "attachments", ()) or ())
        if not attachments:
            return [], None

        candidates = [
            CollectedAttachment(
                filename=getattr(attachment, "filename", "") or "",
                content_type=getattr(attachment, "content_type", None),
                size=int(getattr(attachment, "size", 0) or 0),
                url=str(getattr(attachment, "url", "") or ""),
                message_id=str(getattr(message, "id", "")),
                raw=attachment,
            )
            for attachment in attachments
        ]
        image_candidates = [candidate for candidate in candidates if candidate.looks_like_image()]
        if not image_candidates:
            return [], "_Note: I can't read that attachment type yet — only PNG/JPEG/WebP images are supported._"

        if not self.settings.vision_enabled:
            return [], (
                f"_Note: {len(image_candidates)} image(s) skipped: image analysis is "
                "disabled in configuration._"
            )

        capabilities = await self.provider.capabilities()
        if not (capabilities.exists and capabilities.supports_vision):
            return [], (
                f"_Note: {len(image_candidates)} image(s) skipped: the configured model "
                "does not support vision._"
            )

        try:
            prep = await prepare_images(
                image_candidates,
                max_images=self.settings.max_images,
                max_bytes=self.settings.max_image_bytes,
            )
        except Exception:  # noqa: BLE001 - bounded processing must never break chat
            log.exception("chat: image preparation failed")
            return [], "_Note: I couldn't process the attached image(s)._"

        notes = list(prep.skipped) + list(prep.notes)
        note = f"_Note: {truncate('; '.join(notes), 220)}_" if notes else None
        return [image.data for image in prep.images], note

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
