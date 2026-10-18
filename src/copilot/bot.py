"""The Discord client: minimal intents, cog loading, truthful startup, graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import commands

from .config import Settings
from .core.analyzer import ThreadAnalyzer
from .core.queue import InferenceQueue
from .db.database import Database
from .db.repository import Repository
from .db.retention import sweep_expired
from .errors import CopilotError, new_error_code
from .llm.ollama_provider import OllamaProvider

log = logging.getLogger(__name__)


class CommunityCopilot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True  # slash commands only; no message-content intent is ever needed
        super().__init__(
            command_prefix=commands.when_mentioned_or("cc!"),
            intents=intents,
            help_command=None,
        )

        self.settings = settings
        self.provider = OllamaProvider(settings)
        self.db = Database(settings.db_path)
        self.repo = Repository(self.db)
        self.queue = InferenceQueue(
            capacity=settings.queue_capacity,
            per_user_cooldown_s=settings.per_user_cooldown_s,
            per_guild_hourly_limit=settings.per_guild_hourly_limit,
            job_timeout_s=settings.job_timeout_s,
        )
        self.analyzer = ThreadAnalyzer(self.provider, settings)
        self._retention_task: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.queue.start()

        from .cogs.debug import DebugCog
        from .cogs.health import HealthCog
        from .cogs.insights import InsightsCog
        from .cogs.knowledge import KnowledgeCog
        from .cogs.moderation import ModerationCog
        from .cogs.summary import SummaryCog

        for cog in (
            HealthCog(self),
            SummaryCog(self),
            ModerationCog(self),
            KnowledgeCog(self),
            InsightsCog(self),
            DebugCog(self),
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
            except Exception:  # noqa: BLE001 - sweeps must never kill the bot
                log.exception("Retention sweep failed")
            await asyncio.sleep(24 * 3600)

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
