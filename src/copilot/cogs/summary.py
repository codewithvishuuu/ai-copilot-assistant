"""`/summarize` and `/cancel` — queue + collection + analysis + rendering."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..core import duplicate
from ..core.duplicate import RelatedDoc, find_related
from ..db import retention
from ..errors import (
    CopilotError,
    DatabaseError,
    JobCancelledError,
    new_error_code,
)
from ..ui import checks, embeds
from .base import CopilotCog

log = logging.getLogger(__name__)


class SummaryCog(CopilotCog):
    @app_commands.command(
        name="summarize",
        description="Summarize a forum post/thread: problem, findings, open questions, status, sources",
    )
    @app_commands.describe(
        thread="Thread to summarize (defaults to the thread this command runs in)",
        count="How many recent messages to include (5–500)",
    )
    async def summarize(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
        count: app_commands.Range[int, 5, 500] | None = None,
    ) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return

        target = thread if thread is not None else interaction.channel
        if not isinstance(target, discord.Thread):
            await self.reply(
                interaction,
                content=(
                    "Run this inside a forum post/thread, or pick one with the `thread` option. "
                    "Regular channels have no thread history to summarize."
                ),
            )
            return

        if not await checks.ensure_target_thread(interaction, target, self.settings):
            return

        await interaction.response.defer(ephemeral=self.settings.ephemeral_by_default)

        try:
            job = await self.queue.submit(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                run=lambda: self.analyzer.analyze(
                    target, requested_by=str(interaction.user.id), max_messages=count
                ),
            )
        except CopilotError as exc:
            await self.reply(interaction, content=exc.user_message())
            return
        except Exception:  # noqa: BLE001 - never crash a command
            code = new_error_code()
            log.exception("Failed to enqueue summarize job (%s)", code)
            await self.reply(interaction, content=f"Could not queue the request. `{code}`")
            return

        snapshot = self.queue.snapshot()
        ahead = max(0, snapshot.pending - 1) + (1 if snapshot.active_job_id else 0)
        if ahead > 0:
            await self.reply(
                interaction,
                content=(
                    f"Queued — {ahead} request(s) ahead of yours. "
                    "Local CPU inference can take a while; I'll post the summary here when it's done."
                ),
            )

        try:
            result = await job.future
        except JobCancelledError:
            await self.reply(interaction, content="Summarization cancelled.")
            return
        except CopilotError as exc:
            log.info("Summarize job %s failed: %s", job.id, exc)
            await self.reply(interaction, content=exc.user_message())
            return
        except Exception:  # noqa: BLE001 - never crash a command
            code = new_error_code()
            log.exception("Summarize job %s crashed (%s)", job.id, code)
            await self.reply(interaction, content=f"Unexpected error while summarizing. `{code}`")
            return

        correction = None
        related: list[RelatedDoc] = []
        try:
            thread_row_id = await self._persist(result, requested_by=str(interaction.user.id))
            await self._index_fingerprint(result, thread_row_id)
            correction = await self.repo.latest_correction(thread_row_id)
            related = await find_related(
                self.repo,
                duplicate.fingerprint_text(result.analysis),
                guild_id=result.collected.guild_id,
                exclude_discord_thread_id=result.collected.thread_id,
            )
        except DatabaseError:
            log.exception("Storing the summary failed; still rendering it")
            result.warnings.append("this summary could not be saved to the local database")

        embeds_out = embeds.build_summary_embeds(result, correction=correction, related=related)
        await self.reply(
            interaction, embeds=embeds_out, ephemeral=self.settings.ephemeral_by_default
        )

    # ---------------------------------------------------------------- /cancel

    @app_commands.command(
        name="cancel", description="Cancel your pending or running summarization"
    )
    async def cancel(self, interaction: discord.Interaction) -> None:
        if not await checks.ensure_allowed(interaction, self.settings):
            return
        job = self.queue.find_job_for_user(interaction.user.id)
        if job is None:
            await self.reply(interaction, content="You have no pending or running requests.")
            return
        cancelled = self.queue.cancel(job.id, interaction.user.id)
        await self.reply(
            interaction,
            content="Cancelling your request…" if cancelled else "That request could not be cancelled.",
        )

    # ------------------------------------------------------------- persistence

    async def _persist(self, result, *, requested_by: str) -> int:
        expiry = retention.expiry_from_now(self.settings.retention_days)
        collected = result.collected
        thread_row_id = await self.repo.upsert_thread(
            discord_thread_id=collected.thread_id,
            guild_id=collected.guild_id,
            parent_channel_id=collected.parent_channel_id,
            name=collected.thread_name,
            retention_expires_at=expiry,
        )
        await self.repo.store_messages(thread_row_id, collected.messages)
        await self.repo.insert_summary(
            thread_id=thread_row_id,
            requested_by=requested_by,
            model=result.model,
            prompt_version=result.prompt_version,
            status=result.analysis.current_status.value,
            confidence=result.analysis.status_confidence,
            analysis_json=result.analysis.model_dump_json(),
            error_signatures=result.analysis.error_signatures(),
        )
        await self.repo.touch_thread_summarized(thread_row_id)
        return thread_row_id

    async def _index_fingerprint(self, result, thread_row_id: int) -> None:
        text = duplicate.fingerprint_text(result.analysis)
        if not text.strip():
            return
        if await self.repo.kb_doc_exists(kind="thread", ref_id=thread_row_id):
            await self.repo.delete_kb_docs(kind="thread", ref_id=thread_row_id)
        await self.repo.insert_kb_doc(
            kind="thread",
            ref_id=thread_row_id,
            guild_id=result.collected.guild_id,
            title=result.collected.thread_name or "thread",
            body=text,
            url=result.collected.jump_url or None,
        )
