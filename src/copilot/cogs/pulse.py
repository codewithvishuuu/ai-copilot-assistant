"""Community Pulse commands and the human-reviewed Content Studio bridge.

``/pulse`` is read-only and aggregate-only. ``/pulse-draft`` turns one current,
reproducible recommendation into a Content Studio **draft** and nothing else:
no submit, approval, schedule, or publish call is made here.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core.community_pulse import compute_community_pulse, create_draft_from_recommendation
from ..errors import CopilotError, new_error_code
from ..ui import checks
from ..ui.content import DiscordContentSender
from .base import CopilotCog
from .content import moderator_actor

log = logging.getLogger(__name__)

_MAX_FIELD = 1024


def _clip(text: str, limit: int = _MAX_FIELD) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pulse_embed(pulse) -> discord.Embed:
    """Bounded, honest rendering: observations are facts, recommendations estimates."""
    embed = discord.Embed(
        title=f"Community Pulse — last {pulse.window_days} days",
        colour=discord.Color.blurple(),
        description=(
            f"{pulse.total_threads} summarized thread(s); minimum required: "
            f"{pulse.minimum_threads}. Aggregate data only — no member profiles."
        ),
    )
    if pulse.observations:
        lines = [f"• {item.title} — {item.count}" for item in pulse.observations[:6]]
        embed.add_field(name="Observations (stored data)", value=_clip("\n".join(lines)), inline=False)
    if pulse.recommendations:
        for index, rec in enumerate(pulse.recommendations[:4], start=1):
            evidence = ", ".join(item.title for item in rec.evidence[:2]) or "no evidence reference"
            value = (
                f"{rec.rationale}\n"
                f"Next: {rec.suggested_action}\n"
                f"{rec.expected_outcome}\n"
                f"Uncertainty: {rec.uncertainty} · id: `{rec.recommendation_id}`\n"
                f"Evidence: {evidence}"
            )
            embed.add_field(name=f"{index}. {rec.kind}: {rec.title}", value=_clip(value), inline=False)
    embed.set_footer(text="Estimates are not measured outcomes. A human decides what to do.")
    return embed


class PulseCog(CopilotCog):
    async def _gate(self, interaction: discord.Interaction):
        if not await checks.ensure_allowed(interaction, self.settings):
            return None
        if not await checks.ensure_moderator(interaction, self.settings):
            return None
        return moderator_actor(interaction, self.settings)

    async def _safe_error(self, interaction: discord.Interaction, exc: BaseException) -> None:
        code = new_error_code()
        if isinstance(exc, CopilotError):
            message = exc.user_message()
            log.info("Pulse action error %s: %s", code, exc)
            if exc.detail:
                log.debug("Pulse action error %s detail: %s", code, exc.detail)
        else:
            message = "Unexpected error while preparing community pulse."
            log.exception("Unhandled pulse action error %s: %s", code, exc)
        try:
            await self.reply(interaction, content=f"{message}\n`{code}`")
        except Exception:  # pragma: no cover - interaction may be gone
            log.warning("Could not deliver pulse error %s", code)

    @app_commands.command(
        name="pulse",
        description="Moderator: evidence-backed community observations and recommendations",
    )
    async def pulse(self, interaction: discord.Interaction) -> None:
        if await self._gate(interaction) is None:
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await compute_community_pulse(
                self.repo,
                guild_id=str(interaction.guild_id),
                window_days=self.settings.insights_window_days,
                minimum_threads=self.settings.insights_min_threads,
            )
            if not result.sufficient:
                await self.reply(
                    interaction,
                    content=(
                        f"Not enough summarized threads for a useful Pulse yet "
                        f"({result.total_threads} of {result.minimum_threads} required in "
                        f"{result.window_days} days). Nothing was inferred from too little data."
                    ),
                )
                return
            await self.reply(interaction, embed=pulse_embed(result))
        except Exception as exc:  # noqa: BLE001 - command boundary must stay safe
            await self._safe_error(interaction, exc)

    @app_commands.command(
        name="pulse-draft",
        description="Moderator: create a Content Studio draft from a current Pulse recommendation",
    )
    @app_commands.describe(
        recommendation_id="Id shown by /pulse (recomputed from current data)",
        channel="Intended public channel (defaults to this channel)",
    )
    async def pulse_draft(
        self,
        interaction: discord.Interaction,
        recommendation_id: app_commands.Range[str, 8, 32],
        channel: discord.TextChannel | None = None,
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
            target = await DiscordContentSender(self.bot).resolve_target(
                int(interaction.guild_id), int(target_channel.id)
            )
            view = await create_draft_from_recommendation(
                self.repo,
                self.bot.content_studio,
                actor,
                recommendation_id=recommendation_id,
                target=target,
                settings=self.settings,
            )
            await self.reply(
                interaction,
                content=(
                    f"Created content draft `{view.draft_id}` (version {view.version}) from "
                    f"the Pulse recommendation. This is a draft only: it was not submitted, "
                    "approved, scheduled, or published. Review it with `/content-view`."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - command boundary must stay safe
            await self._safe_error(interaction, exc)
