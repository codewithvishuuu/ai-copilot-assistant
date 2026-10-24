"""The Discord client: minimal intents, cog loading, truthful startup, graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import commands

from .agents.builtin import QualityChecksSkill, build_default_registry
from .agents.host import AgentRunService
from .agents.orchestrator import SkillOrchestrator
from .config import Settings
from .core.analyzer import ThreadAnalyzer
from .core.approvals import ApprovalService
from .core.content_planning import ContentPlanningService
from .core.content_studio import ContentStudio
from .core.evaluation import EvaluationService
from .core.moderation_ai import ModerationService
from .core.publishing import ContentPublisher
from .core.queue import InferenceQueue
from .core.scheduler import SchedulerService
from .db.database import Database
from .db.repository import Repository
from .db.retention import (
    sweep_agent_eval_runs,
    sweep_attachments,
    sweep_content_planning,
    sweep_conversation_turns,
    sweep_expired,
    sweep_missions,
    sweep_reply_approvals,
    sweep_research_reports,
)
from .errors import CopilotError, new_error_code
from .llm.ollama_provider import OllamaProvider
from .missions.service import MissionService
from .research.service import ResearchService
from .ui.checks import is_moderator

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
        # Web research is a configured provider, not a model-chosen tool. The
        # service returns an explicit "unavailable" outcome when no provider is
        # configured instead of fabricating sources.
        self.research_service = ResearchService(self.repo, settings)
        # Phase 1: an explicit read-only registry, not a model-controlled tool
        # surface. Missions and research run only through this registry. Do not
        # register a skill whose provider is disabled: agent/mission plans must
        # report it as unavailable instead of advertising a green capability.
        research_registry_service = (
            None if settings.research_unavailable_reason is not None else self.research_service
        )
        self.moderation_service = ModerationService(self.repo, self.provider, settings)
        self.skill_registry = build_default_registry(
            self.repo,
            insights_window_days=settings.insights_window_days,
            insights_minimum_threads=settings.insights_min_threads,
            research_service=research_registry_service,
            moderation_service=self.moderation_service,
            provider=self.provider,
            settings=settings,
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
        self.mission_service = MissionService(self.repo, self.orchestrator, settings)
        self.agent_service = AgentRunService(self.repo, self.orchestrator, settings)
        # Durable jobs may only start read-only agent runs or prepare drafts;
        # there is deliberately no publish job kind.
        self.scheduler = SchedulerService(
            self.repo,
            settings,
            agent_service=self.agent_service,
            content_studio=self.content_studio,
            provider=self.provider,
            authorize=self._scheduled_requester_authorized,
        )
        self.evaluation_service = EvaluationService(self.repo, self.orchestrator, settings)
        # Registered after the service exists; the skill shares the same checks as
        # /evaluate but never persists a report or writes audit.
        self.skill_registry.register(QualityChecksSkill(self.evaluation_service))
        self.content_planning = ContentPlanningService(self.repo, settings)
        self._retention_task: asyncio.Task | None = None

    async def _scheduled_requester_authorized(
        self, guild_id: int, channel_id: int, requester_id: int
    ) -> bool:
        """Live moderator recheck for a scheduled job's original requester.

        The scheduler calls this at execution time; cached permissions from the
        scheduling interaction are never trusted. A confirmed missing member
        denies; an unavailable guild/member lookup raises so the job is retried.
        """
        guild = self.get_guild(int(guild_id))
        if guild is None:
            # The gateway cache is empty at boot (and briefly on reconnect). Raise
            # so the scheduler retries instead of recording a permanent revocation.
            raise RuntimeError("guild cache is not ready")
        member = guild.get_member(int(requester_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(requester_id))
            except discord.NotFound:
                return False  # the requester really left this guild
            except Exception as exc:  # noqa: BLE001 - transient lookup failure
                raise RuntimeError("member lookup failed") from exc
        permissions = getattr(member, "guild_permissions", None)
        roles = [role.id for role in getattr(member, "roles", []) if hasattr(role, "id")]
        return is_moderator(permissions, roles, self.settings.moderator_role_ids)

    # ------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.queue.start()

        from .cogs.agents import AgentCog
        from .cogs.automod import AutomodCog
        from .cogs.content import ContentCog
        from .cogs.conversation import ConversationCog
        from .cogs.debug import DebugCog
        from .cogs.evaluation import EvaluationCog
        from .cogs.health import HealthCog
        from .cogs.insights import InsightsCog
        from .cogs.knowledge import KnowledgeCog
        from .cogs.missions import MissionCog
        from .cogs.moderation import ModerationCog
        from .cogs.planning import PlanningCog
        from .cogs.pulse import PulseCog
        from .cogs.radar import RadarCog
        from .cogs.research import ResearchCog
        from .cogs.scheduler import SchedulerCog
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
            RadarCog(self),
            ResearchCog(self),
            MissionCog(self),
            AgentCog(self),
            SchedulerCog(self),
            PlanningCog(self),
            EvaluationCog(self),
            AutomodCog(self),
        ):
            await self.add_cog(cog)

        interrupted = await self.mission_service.recover_interrupted()
        if interrupted:
            log.warning("Recovered %d mission(s) interrupted by a previous shutdown", interrupted)

        interrupted_agents = await self.agent_service.recover_interrupted()
        if interrupted_agents:
            log.warning(
                "Recovered %d agent run(s) interrupted by a previous shutdown", interrupted_agents
            )

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
        # Start due-job execution only after the gateway cache is populated.
        # setup_hook runs before connect(), so starting there made boot-time jobs
        # fail their live moderator check and become permanently revoked.
        await self.scheduler.start()
        research_reason = self.settings.research_unavailable_reason
        if research_reason:
            log.info("Web research is unavailable: %s", research_reason)
        else:
            log.info(
                "Web research is configured (provider=%s)", self.settings.web_search_provider
            )
        if not self.settings.content_publishing_enabled:
            log.info("Content publishing is disabled by configuration (drafts still work)")
        if self.settings.moderation_enabled:
            log.warning(
                "AI moderation is enabled globally but the Message Content intent is OFF: "
                "only messages that explicitly mention the bot can be assessed. Ordinary "
                "channel messages are out of scope. Per-guild policies still need a private "
                "report channel."
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
        await self.scheduler.stop()
        await self.agent_service.shutdown()
        await self.mission_service.shutdown()
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
                attachments = await sweep_attachments(
                    self.repo, days=self.settings.attachment_retention_days
                )
                if attachments:
                    log.info("Retention sweep removed %d attachment row(s)", attachments)
                expired = await self.approval_service.expire_due()
                if expired:
                    log.info("Retention sweep expired %d approval request(s)", expired)
                reply_expired = await sweep_reply_approvals(self.repo)
                if reply_expired:
                    log.info("Retention sweep expired %d reply approval(s)", reply_expired)
                missions = await sweep_missions(self.db, days=self.settings.retention_days)
                reports = await sweep_research_reports(self.db, days=self.settings.retention_days)
                evals = await sweep_agent_eval_runs(self.db, days=self.settings.retention_days)
                planning = await sweep_content_planning(self.db, days=self.settings.retention_days)
                if missions or reports or evals or planning:
                    log.info(
                        "Retention sweep removed %d mission(s), %d research report(s), "
                        "%d evaluation(s), %d planning row(s)",
                        missions,
                        reports,
                        evals,
                        planning,
                    )
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
