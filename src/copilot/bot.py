"""The Discord client: minimal intents, cog loading, truthful startup, graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import commands

from .agents.builtin import build_default_registry
from .agents.orchestrator import SkillOrchestrator
from .config import Settings
from .core.analyzer import ThreadAnalyzer
from .core.approvals import ApprovalService
from .core.content_studio import ContentStudio
from .core.publishing import ContentPublisher
from .core.queue import InferenceQueue
from .db.database import Database
from .db.repository import Repository
from .db.retention import sweep_conversation_turns, sweep_expired
from .errors import CopilotError, new_error_code
from .llm.ollama_provider import OllamaProvider

log = logging.getLogger(__name__)


class CommunityCopilot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # Message events (non-privileged) power mention-triggered chat. The
        # privileged Message Content intent stays OFF: Discord only exposes
        # message text for mentions / DMs / the bot's own messages.
        intents.guild_messages = True
        super().__init__(
            command_prefix=commands.when_mentioned_or("cc!"),
            intents=intents,
            help_command=None,
        )

        self.settings = settings
        self.provider = OllamaProvider(settings)
        self.db = Database(settings.db_path)
        self.repo = Repository(self.db)
        # Phase 1: an explicit read-only registry, not a model-controlled tool
        # surface. No command calls it yet; future adapters must use this path.
        self.skill_registry = build_default_registry(
            self.repo,
            insights_window_days=settings.insights_window_days,
            insights_minimum_threads=settings.insights_min_threads,
        )
        self.orchestrator = SkillOrchestrator(
            self.skill_registry,
            allowed_guild_ids=settings.guild_allowlist,
            allowed_channel_ids=settings.channel_allowlist,
        )
        self.queue = InferenceQueue(
            capacity=settings.queue_capacity,
            per_user_cooldown_s=settings.per_user_cooldown_s,
            per_guild_hourly_limit=settings.per_guild_hourly_limit,
            job_timeout_s=settings.job_timeout_s,
        )
        # Phase 2A: backend approval state. Phase 2B uses it for every content
        # publication; there is still no model tool or generic public executor.
        self.approval_service = ApprovalService(self.repo)
        self.content_studio = ContentStudio(self.repo, self.approval_service, settings=settings)
        self.content_publisher = ContentPublisher(
            self.content_studio, self.approval_service, self.repo, settings=settings
        )
        self.analyzer = ThreadAnalyzer(self.provider, settings)
        self._retention_task: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.queue.start()

        from .cogs.content import ContentCog
        from .cogs.conversation import ConversationCog
        from .cogs.debug import DebugCog
        from .cogs.health import HealthCog
        from .cogs.insights import InsightsCog
        from .cogs.knowledge import KnowledgeCog
        from .cogs.moderation import ModerationCog
        from .cogs.pulse import PulseCog
        from .cogs.summary import SummaryCog

        for cog in (
            HealthCog(self),
            SummaryCog(self),
            ModerationCog(self),
            KnowledgeCog(self),
            InsightsCog(self),
            DebugCog(self),
            ConversationCog(self),
            ContentCog(self),
            PulseCog(self),
        ):
            await self.add_cog(cog)

        self.tree.on_error = self._on_app_command_error

        try:
            if self.settings.test_guild_id:
                guild = discord.Object(id=self.settings.test_guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "Synced %d command(s) to test guild %s",
                    len(synced),
                    self.settings.test_guild_id,
                )
            else:
                synced = await self.tree.sync()
                log.info(
                    "Synced %d global command(s); global sync can take up to an hour to appear",
                    len(synced),
                )
        except discord.HTTPException as exc:
            log.warning("Command sync failed (will retry on restart): %s", exc)

        self._retention_task = asyncio.create_task(
            self._retention_loop(), name="copilot-retention"
        )

    async def on_ready(self) -> None:
        user = self.user
        log.info(
            "Logged in as %s (id=%s), %d guild(s) visible",
            user,
            getattr(user, "id", "?"),
            len(self.guilds),
        )
        health = await self.provider.health()
        if not health.reachable:
            log.warning(
                "Ollama is not reachable at %s — /summarize will fail until it is running",
                self.settings.ollama_base_url,
            )
            return
        capabilities = await self.provider.capabilities()
        if capabilities.exists:
            log.info(
                "Model %s ready (vision=%s, context=%s)",
                capabilities.name,
                capabilities.supports_vision,
                capabilities.context_length,
            )
        else:
            log.warning(
                "Model %s is not usable (%s) — check OLLAMA_MODEL and run `ollama pull`",
                self.settings.ollama_model,
                capabilities.error,
            )

    async def close(self) -> None:
        if self._retention_task is not None:
            self._retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retention_task
            self._retention_task = None
        await self.queue.stop()
        await self.db.close()
        await self.provider.aclose()
        await super().close()

    # -------------------------------------------------------------- internals

    async def _retention_loop(self) -> None:
        while True:
            try:
                counts = await sweep_expired(self.db, days=self.settings.retention_days)
                if any(counts.values()):
                    log.info("Retention sweep removed: %s", counts)
                turns = await sweep_conversation_turns(
                    self.db, days=self.settings.conversation_retention_days
                )
                if turns:
                    log.info("Retention sweep removed %d conversation turn(s)", turns)
                expired = await self.approval_service.expire_due()
                if expired:
                    log.info("Retention sweep expired %d approval request(s)", expired)
            except Exception:  # noqa: BLE001 - sweeps must never kill the bot
                log.exception("Retention sweep failed")
            await asyncio.sleep(24 * 3600)

    async def on_command_error(self, context: commands.Context, error: commands.CommandError) -> None:
        """Prefix machinery stays quiet: this bot is slash-only.

        ``command_prefix`` keeps the mention/``cc!`` forms for compatibility, and
        mention messages now flow through the command parser too (message events
        are enabled for chat) — unknown "commands" must not spam logs or users.
        """
        if isinstance(error, commands.CommandNotFound):
            return
        log.warning("Ignoring prefix-command error: %s", type(error).__name__)

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: BaseException
    ) -> None:
        original = getattr(error, "original", error)
        code = new_error_code()
        if isinstance(original, CopilotError):
            message = f"{original.user_message()}\n`{code}`"
            log.info("Command error %s: %s", code, original)
            if original.detail:
                log.debug("Error %s detail: %s", code, original.detail)
        else:
            message = f"Unexpected error. `{code}`"
            log.exception("Unhandled command error %s: %s", code, error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not deliver error message for %s", code)
