"""Embeds for durable scheduled jobs.

The scheduler only starts read-only agent runs or prepares Content Studio
drafts. These views state that explicitly and never imply a message was sent.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

from ..core.scheduler import ScheduledJobView
from .embeds import FIELD_NAME_LIMIT, FIELD_VALUE_LIMIT, GREEN, ORANGE, RED, clip

BLURPLE = 0x5865F2
GREY = 0x95A5A6

_COLORS = {
    "pending": ORANGE,
    "running": BLURPLE,
    "succeeded": GREEN,
    "failed": RED,
    "cancelled": GREY,
}

_KIND_LABELS = {
    "agent.run": "Read-only agent run",
    "digest.collect": "Community digest analysis",
    "content.draft": "Content draft (needs approval)",
}


def scheduled_job_embed(job: ScheduledJobView) -> discord.Embed:
    embed = discord.Embed(
        title=f"Scheduled job #{job.job_id} · {_KIND_LABELS.get(job.kind.value, job.kind.value)}",
        description=clip(job.summary or "—", 4096),
        colour=_COLORS.get(job.status, GREY),
    )
    status = (
        f"**{job.status}** · attempt {job.attempts}/{job.max_attempts}\n"
        f"scheduled {job.run_at} ({job.timezone})\n"
        f"created {job.created_at} by `{job.created_by}`"
    )
    embed.add_field(name="Status", value=clip(status, FIELD_VALUE_LIMIT), inline=False)
    if job.last_error:
        embed.add_field(
            name="Last failure",
            value=clip(
                f"`{job.last_error}` — the job stopped safely and did not send or publish anything.",
                FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )
    if job.started_at or job.finished_at:
        embed.add_field(
            name="Timeline",
            value=clip(
                f"started: {job.started_at or 'not started'}\n"
                f"finished: {job.finished_at or 'not finished'}",
                FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )
    embed.set_footer(
        text="The scheduler never posts or publishes. Agent runs are read-only; drafts still need approval."
    )
    return embed


def scheduled_job_list_embed(jobs: Sequence[ScheduledJobView]) -> discord.Embed:
    embed = discord.Embed(
        title="Scheduled jobs",
        colour=BLURPLE if jobs else GREY,
    )
    if not jobs:
        embed.description = (
            "No scheduled jobs. Create one with `/schedule-agent`, `/schedule-draft` or "
            "`/schedule-digest`; nothing is posted automatically."
        )
        return embed
    for job in jobs[:20]:
        value = (
            f"{job.status} · {job.run_at} ({job.timezone}) · attempt {job.attempts}/{job.max_attempts}\n"
            f"{job.summary or '—'}"
        )
        if job.last_error:
            value += f"\nlast error: `{job.last_error}`"
        embed.add_field(
            name=clip(f"#{job.job_id} · {_KIND_LABELS.get(job.kind.value, job.kind.value)}", FIELD_NAME_LIMIT),
            value=clip(value, FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(
        text="Jobs are persisted and recover after restart. Nothing is posted or published by the scheduler."
    )
    return embed
