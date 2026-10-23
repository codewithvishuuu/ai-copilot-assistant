"""Embeds for Main Host Agent runs and Agent Control.

These are read-only views over persisted, guild-scoped agent runs. They never
trigger work and never imply that a public action happened: the host delegates
only to read-only skills, and publishing/approvals stay human-gated elsewhere.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

from ..agents.host import AgentControlView, AgentRunStatus, AgentRunView
from ..agents.roles import ROLE_DEFINITIONS
from .embeds import FIELD_NAME_LIMIT, FIELD_VALUE_LIMIT, GREEN, ORANGE, RED, clip

BLURPLE = 0x5865F2
GREY = 0x95A5A6
YELLOW = 0xFEE75C

_AGENT_COLORS = {
    AgentRunStatus.planned: ORANGE,
    AgentRunStatus.running: BLURPLE,
    AgentRunStatus.completed: GREEN,
    AgentRunStatus.partial: YELLOW,
    AgentRunStatus.failed: RED,
    AgentRunStatus.cancelled: GREY,
}

_STEP_ICONS = {
    "pending": "⚪",
    "running": "🔄",
    "succeeded": "✅",
    "failed": "❌",
    "skipped": "⏭️",
    "cancelled": "🚫",
}

_ROLE_TITLES = {role.role_id.value: role.title for role in ROLE_DEFINITIONS}


def _safe_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    candidate = url.strip().replace("\n", "").replace("\r", "")
    if not candidate.startswith(("https://", "http://")):
        return None
    return candidate.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _link(title: str, url: object) -> str:
    safe = _safe_url(url)
    label = clip(title or "untitled", 100)
    return f"[{label}]({safe})" if safe else label


def _step_digest(output: dict | None) -> str:
    """One bounded human-readable line from a skill's structured output."""
    if not output:
        return ""
    for field in ("brief", "summary", "answer", "analysis", "text", "synthesis", "pulse"):
        value = output.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            pairs = [
                f"{key}={value[key]}"
                for key in sorted(value)
                if isinstance(value[key], (int, float, bool))
            ]
            if pairs:
                return ", ".join(pairs[:4])
    pairs = [
        f"{key}={value}"
        for key, value in list(output.items())[:4]
        if isinstance(value, (int, float, bool))
        or (isinstance(value, str) and len(value) <= 80)
    ]
    return ", ".join(pairs)


def agent_run_embed(view: AgentRunView) -> discord.Embed:
    embed = discord.Embed(
        title=f"Agent run #{view.run_id} · {view.mode}",
        description=clip(view.goal, 4096),
        colour=_AGENT_COLORS.get(view.status, BLURPLE),
    )
    status = f"**{view.status.value}** · {view.progress_text()}"
    if view.cancel_requested and view.status is AgentRunStatus.running:
        status += "\ncancellation requested"
    status += f"\nTrace `{view.trace_id}` · created {view.created_at}"
    embed.add_field(name="Status", value=clip(status, FIELD_VALUE_LIMIT), inline=False)
    if view.error_code:
        embed.add_field(
            name="Failure",
            value=clip(
                f"`{view.error_code}` — the run stopped safely; step outcomes below stay recorded.",
                FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )

    for step in view.steps:
        icon = _STEP_ICONS.get(step.status.value, "•")
        role = _ROLE_TITLES.get(step.role_id, step.role_id)
        depends = ", ".join(str(dep) for dep in step.depends_on) or "none"
        lines = [f"{icon} {step.status.value} · `{step.skill_id}` · depends on: {depends}"]
        digest = _step_digest(step.output)
        if digest:
            lines.append(clip(digest, 600))
        if step.error:
            lines.append(f"Error: {step.error}")
        links = [
            _link(str(reference.get("title") or "evidence"), reference.get("url"))
            for reference in step.references[:4]
            if isinstance(reference, dict)
        ]
        if links:
            lines.append("Evidence: " + "; ".join(links))
        embed.add_field(
            name=clip(f"{step.index + 1}. {step.title} · {role}", FIELD_NAME_LIMIT),
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )

    if view.summary:
        embed.add_field(
            name="Trace summary",
            value=clip(view.summary, FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(
        text="Read-only host plan over registered skills. Nothing was published or approved."
    )
    return embed


def agent_list_embed(views: Sequence[AgentRunView]) -> discord.Embed:
    embed = discord.Embed(
        title="Agent runs in this server",
        colour=BLURPLE if views else GREY,
    )
    if not views:
        embed.description = (
            "No agent runs yet. Start one with `/agent`; runs are read-only and never publish."
        )
        return embed
    for view in views[:20]:
        embed.add_field(
            name=clip(f"#{view.run_id} · {view.mode} · {view.status.value}", FIELD_NAME_LIMIT),
            value=clip(
                f"{view.progress_text()} · created {view.created_at}\n{clip(view.goal, 300)}",
                FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )
    embed.set_footer(text="Open one with /agent-view. Progress is persisted per step.")
    return embed


def agent_control_embed(control: AgentControlView) -> discord.Embed:
    total = sum(control.counts.values())
    embed = discord.Embed(
        title="Agent Control",
        colour=BLURPLE if total else GREY,
        description=(
            f"{total} agent run(s) recorded in this server · guild `{control.guild_id}`.\n"
            "Read-only control surface: plans delegate to registered read-only skills only, and "
            "the only action here is a moderator cancellation."
        ),
    )
    counts = "\n".join(
        f"{status}: {control.counts.get(status, 0)}"
        for status in ("planned", "running", "completed", "partial", "failed", "cancelled")
    )
    embed.add_field(name="Counts", value=clip(counts, FIELD_VALUE_LIMIT), inline=True)

    if control.providers:
        provider_lines = [f"{name}: {value}" for name, value in sorted(control.providers.items())]
        embed.add_field(
            name="Providers",
            value=clip("\n".join(provider_lines), FIELD_VALUE_LIMIT),
            inline=True,
        )

    role_lines = []
    for role in control.roles:
        badge = "✅" if role.get("available") else "⚠️"
        skills = ", ".join(role.get("skill_ids") or []) or "implemented outside the host"
        line = f"{badge} **{role.get('title')}** — {skills}"
        if role.get("requires_human_approval"):
            line += " · human approval"
        if not role.get("available") and role.get("note"):
            line += f"\n{role['note']}"
        role_lines.append(line)
    embed.add_field(
        name="Specialist roles",
        value=clip("\n".join(role_lines), FIELD_VALUE_LIMIT),
        inline=False,
    )

    if control.recent:
        lines = [
            f"• #{view.run_id} {view.mode} — {view.status.value} ({view.progress_text()}), "
            f"{view.finished_at or 'not finished'}"
            for view in control.recent[:5]
        ]
        embed.add_field(
            name="Recent runs",
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    else:
        embed.add_field(name="Recent runs", value="No agent runs recorded yet.", inline=False)

    embed.set_footer(
        text="Unavailable roles are reported, not simulated. No secrets or member content shown."
    )
    return embed
