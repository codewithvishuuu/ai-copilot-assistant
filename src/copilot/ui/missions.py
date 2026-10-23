"""Embeds for missions, Mission Control, Radar, research, planning and evaluation.

All text is bounded and mention-neutralized; call sites additionally send with
``allowed_mentions=none``. Unavailable, empty and partial states are rendered
explicitly so the UI never implies that a deferred integration ran.

Nothing here performs I/O or takes an action: these are read-only views over
typed snapshots produced by the services.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import discord

from ..core.content_planning import CalendarEntryView, ContextualReplyView, EventPlanView
from ..core.evaluation import EvaluationReport
from ..core.problem_radar import RadarSnapshot
from ..missions.models import MissionControlView, MissionStatus, MissionView
from ..research.service import ResearchOutcome, ResearchStatus
from ..util.text import neutralize_mentions, truncate
from .embeds import FIELD_NAME_LIMIT, FIELD_VALUE_LIMIT, GREEN, ORANGE, RED, clip

BLURPLE = 0x5865F2
GREY = 0x95A5A6

_MISSION_COLORS = {
    MissionStatus.planned: ORANGE,
    MissionStatus.running: BLURPLE,
    MissionStatus.completed: GREEN,
    MissionStatus.failed: RED,
    MissionStatus.cancelled: GREY,
}

_STEP_ICONS = {
    "pending": "⚪",
    "running": "🔄",
    "succeeded": "✅",
    "failed": "❌",
    "skipped": "⏭️",
    "cancelled": "🚫",
}

_PROBLEM_LABELS = {
    "repeated_question": "Repeated question",
    "recurring_problem": "Recurring problem",
    "unresolved_threads": "Open threads",
}


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url.strip().replace("\n", "").replace("\r", "")
    if not candidate.startswith(("https://", "http://")):
        return None
    # Parentheses break masked links; percent-encode the ones that appear.
    return candidate.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _link(title: str, url: str | None) -> str:
    safe = _safe_url(url)
    label = clip(title, 100)
    return f"[{label}]({safe})" if safe else label


def _evidence_lines(evidence: Sequence, limit: int = 3) -> list[str]:
    lines: list[str] = []
    for item in evidence[:limit]:
        title = getattr(item, "title", "")
        kind = getattr(item, "kind", "")
        url = getattr(item, "url", None)
        prefix = f"• {kind}: " if kind else "• "
        lines.append(prefix + _link(title, url))
    return lines


# ------------------------------------------------------------------- missions


def mission_embed(view: MissionView) -> discord.Embed:
    embed = discord.Embed(
        title=f"Mission {view.mission_id} · {view.mode.value}",
        description=clip(view.goal, 4096),
        colour=_MISSION_COLORS.get(view.status, BLURPLE),
    )
    status = f"**{view.status.value}** · {view.progress_text()}"
    if view.cancel_requested and view.status is MissionStatus.running:
        status += "\ncancellation requested"
    trace = f"\nTrace `{view.trace_id}` · created {view.created_at}"
    embed.add_field(name="Status", value=clip(status + trace, FIELD_VALUE_LIMIT), inline=False)
    if view.error_code:
        embed.add_field(
            name="Failure",
            value=clip(
                f"`{view.error_code}` — the mission stopped safely; completed steps stay "
                "recorded above.",
                FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )

    for step in view.steps:
        icon = _STEP_ICONS.get(step.status.value, "•")
        detail = step.output_summary or step.error or "Waiting to run."
        if step.status.value == "running":
            detail = "Running now. " + detail
        value = clip(f"{icon} {step.status.value}\n{detail}", FIELD_VALUE_LIMIT)
        name = clip(f"{step.index + 1}. {step.title} · `{step.skill_id}`", FIELD_NAME_LIMIT)
        embed.add_field(name=name, value=value or "—", inline=False)

    if view.references:
        lines = [_link(reference.title, reference.url) for reference in view.references[:8]]
        embed.add_field(
            name="Evidence references",
            value=clip("\n".join(f"• {line}" for line in lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    if view.summary:
        embed.add_field(
            name="Mission result",
            value=clip(view.summary, 1000),
            inline=False,
        )
    embed.set_footer(
        text="Read-only mission: no message was posted, no approval was granted. "
        "Evidence is limited to the steps above."
    )
    return embed


def mission_list_embed(views: Sequence[MissionView]) -> discord.Embed:
    embed = discord.Embed(
        title="Missions in this server",
        colour=BLURPLE if views else GREY,
    )
    if not views:
        embed.description = (
            "No missions yet. Start one with `/mission`. Missions are read-only and never post."
        )
        return embed
    for view in views[:20]:
        icon = _STEP_ICONS.get("succeeded" if view.status is MissionStatus.completed else "", "")
        value = (
            f"{view.status.value} · {view.progress_text()}\n"
            f"{clip(view.goal, 300)}\n"
            f"created {view.created_at} · id `{view.mission_id}`"
        )
        name = clip(f"{icon} #{view.mission_id} · {view.mode.value}", FIELD_NAME_LIMIT)
        embed.add_field(name=name, value=clip(value, FIELD_VALUE_LIMIT), inline=False)
    embed.set_footer(text="Open one with /mission-view; progress is persisted per step.")
    return embed


def mission_control_embed(control: MissionControlView) -> discord.Embed:
    counts = control.counts
    total = sum(counts.values())
    embed = discord.Embed(
        title="Mission Control",
        colour=BLURPLE if total else GREY,
        description=(
            f"{total} mission(s) recorded in this server · guild `{control.guild_id}`.\n"
            "Read-only control surface: the only action here is a moderator cancellation."
        ),
    )
    count_lines = [
        f"{status}: {counts.get(status, 0)}"
        for status in ("planned", "running", "completed", "failed", "cancelled")
    ]
    embed.add_field(name="Counts", value="\n".join(count_lines), inline=True)
    provider_lines = [
        f"{name}: {value}" for name, value in sorted(control.providers.items())
    ] or ["no provider state available"]
    embed.add_field(name="Providers", value=clip("\n".join(provider_lines), FIELD_VALUE_LIMIT), inline=True)

    if control.active:
        lines = []
        for view in control.active[:5]:
            active = view.active_step()
            step = f" · now: {active.title}" if active is not None else ""
            lines.append(
                f"• #{view.mission_id} {view.mode.value} — {view.status.value} "
                f"({view.progress_text()}{step})"
            )
        embed.add_field(name="Active", value=clip("\n".join(lines), FIELD_VALUE_LIMIT), inline=False)
    else:
        embed.add_field(name="Active", value="No mission is currently planned or running.", inline=False)

    if control.recent:
        lines = [
            f"• #{view.mission_id} {view.mode.value} — {view.status.value} "
            f"({view.progress_text()}), {view.finished_at or 'no finish time'}"
            for view in control.recent[:5]
        ]
        embed.add_field(name="Recently finished", value=clip("\n".join(lines), FIELD_VALUE_LIMIT), inline=False)

    if control.recent_activity:
        lines = [
            f"• {item.created_at}: `{item.action}`"
            + (f" → {item.target_type} {item.target_id}" if item.target_type else "")
            for item in control.recent_activity[:8]
        ]
        embed.add_field(name="Recent audit activity", value=clip("\n".join(lines), FIELD_VALUE_LIMIT), inline=False)

    embed.set_footer(
        text="Providers show configuration/availability only. No secret or member content is shown."
    )
    return embed


# --------------------------------------------------------------------- radar


def radar_embed(snapshot: RadarSnapshot) -> discord.Embed:
    if not snapshot.sufficient:
        embed = discord.Embed(
            title=f"Problem Radar — last {snapshot.window_days} days",
            colour=ORANGE,
            description=(
                f"Not enough summarized threads yet: **{snapshot.total_threads}** of "
                f"**{snapshot.minimum_threads}** required. Nothing was inferred from too little data."
            ),
        )
        if snapshot.notes:
            embed.add_field(
                name="Notes",
                value=clip("\n".join(f"• {note}" for note in snapshot.notes), FIELD_VALUE_LIMIT),
                inline=False,
            )
        return embed

    embed = discord.Embed(
        title=f"Problem Radar — last {snapshot.window_days} days",
        colour=BLURPLE,
        description=clip(
            f"{snapshot.total_threads} summarized thread(s). {snapshot.coverage_summary}",
            4096,
        ),
    )
    for problem in snapshot.problems:
        lines = [
            f"{_PROBLEM_LABELS.get(problem.kind, problem.kind)} ×{problem.count} · "
            f"confidence {problem.confidence} · KB {problem.kb_status}",
        ]
        evidence = _evidence_lines(problem.evidence)
        if evidence:
            lines.append("Evidence:")
            lines.extend(evidence)
        lines.append(f"Uncertainty: {problem.uncertainty}")
        if problem.suggested_action:
            lines.append(f"Suggested action: {problem.suggested_action}")
        lines.append(f"`{problem.problem_id}`")
        embed.add_field(
            name=clip(problem.title, FIELD_NAME_LIMIT),
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    for gap in snapshot.gaps:
        lines = [f"Recurring topic ×{gap.count} · confidence {gap.confidence}"]
        lines.extend(_evidence_lines(gap.evidence))
        if gap.note:
            lines.append(f"Note: {gap.note}")
        if gap.suggested_action:
            lines.append(f"Suggested action: {gap.suggested_action}")
        lines.append(f"`{gap.gap_id}`")
        embed.add_field(
            name=clip(f"Knowledge gap: {gap.title}", FIELD_NAME_LIMIT),
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    if snapshot.recommendations:
        lines = [
            f"• {rec.title} — {rec.suggested_action} (uncertainty: {rec.uncertainty})"
            for rec in snapshot.recommendations[:3]
        ]
        embed.add_field(
            name="Recommendations (estimates — a human decides)",
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    if snapshot.notes:
        embed.add_field(
            name="Method",
            value=clip("\n".join(f"• {note}" for note in snapshot.notes), FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(text="Evidence is stored, guild-scoped data. No live channels were scraped.")
    return embed


# ------------------------------------------------------------------ research


def research_embed(outcome: ResearchOutcome) -> discord.Embed:
    if outcome.status is ResearchStatus.unavailable:
        embed = discord.Embed(
            title="Web research unavailable",
            colour=ORANGE,
            description=clip(
                (outcome.reason or "No web search provider is configured.")
                + "\n\nNo search was performed and no citations were fabricated.",
                4096,
            ),
        )
        embed.set_footer(text=f"provider: {outcome.provider} · report {outcome.report_id or 'not saved'}")
        return embed
    if outcome.status is ResearchStatus.failed:
        embed = discord.Embed(
            title="Web research failed safely",
            colour=RED,
            description=clip(
                (outcome.reason or "The provider did not return usable results.")
                + "\n\nNo summary or citations were invented.",
                4096,
            ),
        )
        embed.set_footer(text=f"provider: {outcome.provider} · report {outcome.report_id or 'not saved'}")
        return embed

    synthesis = (
        "model synthesis with mandated [n] citations"
        if outcome.synthesis == "model"
        else "extractive summary of search snippets (no model synthesis)"
    )
    embed = discord.Embed(
        title=f"Research: {clip(outcome.query, 240)}",
        colour=BLURPLE,
        description=clip(outcome.summary, 3500),
    )
    embed.add_field(name="Provider", value=clip(outcome.provider, FIELD_VALUE_LIMIT), inline=True)
    embed.add_field(name="Synthesis", value=clip(synthesis, FIELD_VALUE_LIMIT), inline=True)
    embed.add_field(name="Report", value=f"`{outcome.report_id}`", inline=True)
    if outcome.sources:
        lines = [
            f"[{index}] {_link(source.title, source.url)}"
            for index, source in enumerate(outcome.sources[:8], start=1)
        ]
        embed.add_field(
            name="Sources (web search results)",
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(
        text="Citations point at search results, not fetched pages. Verify before relying on a claim."
    )
    return embed


# ---------------------------------------------------------------- evaluation


def evaluation_embed(report: EvaluationReport) -> discord.Embed:
    embed = discord.Embed(
        title=f"Agent evaluation {report.evaluation_id or ''}".strip(),
        colour=GREEN if report.passed else RED,
        description=clip(
            f"{report.summary}\n\n"
            "This suite checks contracts, permissions, guild isolation, approvals and "
            "migrations offline. It does not measure model answer quality.",
            4096,
        ),
    )
    embed.add_field(name="Passed", value=str(report.passed_count), inline=True)
    embed.add_field(name="Failed", value=str(report.failed_count), inline=True)
    embed.add_field(
        name="Scope",
        value="offline only · no model quality evaluation",
        inline=True,
    )
    failed = [check for check in report.checks if not check.passed]
    for check in failed[:10]:
        embed.add_field(
            name=clip(f"❌ {check.check_id} · {check.category}", FIELD_NAME_LIMIT),
            value=clip(f"{check.title}\n{check.detail}", FIELD_VALUE_LIMIT),
            inline=False,
        )
    passed = [check for check in report.checks if check.passed]
    if passed:
        lines = [f"✅ {check.check_id} — {check.title}" for check in passed[:12]]
        if len(passed) > 12:
            lines.append(f"…and {len(passed) - 12} more")
        embed.add_field(
            name="Passing checks",
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(text=clip(report.disclaimer, 2048))
    return embed


# ------------------------------------------------------- content planning


def event_plan_embed(plan: EventPlanView) -> discord.Embed:
    embed = discord.Embed(
        title=f"Event plan {plan.plan_id} · {plan.state}",
        colour=BLURPLE,
        description=clip(plan.brief, 4096),
    )
    if plan.scheduled_for:
        embed.add_field(name="Proposed time", value=clip(plan.scheduled_for, FIELD_VALUE_LIMIT), inline=True)
    if plan.target_channel_id:
        embed.add_field(name="Proposed channel", value=f"<#{plan.target_channel_id}>", inline=True)
    embed.add_field(name="Created by", value=f"`{plan.created_by}`", inline=True)
    if plan.agenda:
        embed.add_field(
            name="Agenda (template)",
            value=clip("\n".join(f"• {item}" for item in plan.agenda), FIELD_VALUE_LIMIT),
            inline=False,
        )
    if plan.checklist:
        embed.add_field(
            name="Checklist",
            value=clip("\n".join(f"☐ {item}" for item in plan.checklist), FIELD_VALUE_LIMIT),
            inline=False,
        )
    if plan.roles:
        embed.add_field(
            name="Suggested roles",
            value=clip("\n".join(f"• {item}" for item in plan.roles), FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(
        text="Planning template only. No Discord event, announcement or reminder was created or sent."
    )
    return embed


def event_plan_list_embed(plans: Sequence[EventPlanView]) -> discord.Embed:
    embed = discord.Embed(title="Event plans", colour=BLURPLE if plans else GREY)
    if not plans:
        embed.description = "No event plans yet. Create one with `/event-plan`."
        return embed
    for plan in plans[:20]:
        when = plan.scheduled_for or "no proposed time"
        embed.add_field(
            name=clip(f"#{plan.plan_id} · {plan.state} · {plan.title}", FIELD_NAME_LIMIT),
            value=clip(f"{when}\ncreated {plan.created_at}", FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(text="Open one with /event-plan-view. Plans never schedule anything.")
    return embed


def calendar_embed(
    entries: Sequence[CalendarEntryView],
    *,
    include_cancelled: bool,
    jobs: Mapping[int, Any] | None = None,
) -> discord.Embed:
    title = "Content calendar" + (" (including cancelled)" if include_cancelled else "")
    embed = discord.Embed(title=title, colour=BLURPLE if entries else GREY)
    if not entries:
        embed.description = (
            "No planned entries. Add one with `/calendar-add`; entries never schedule a post "
            "by themselves, and `/calendar-schedule` creates a durable job only for read-only "
            "analysis or an approval-gated draft."
        )
        return embed
    jobs = jobs or {}
    for entry in entries[:20]:
        lines = [f"{entry.kind.value} · {entry.state.value}"]
        when = entry.starts_at + (f" → {entry.ends_at}" if entry.ends_at else "")
        lines.append(when)
        if entry.target_channel_id:
            lines.append(f"Channel: <#{entry.target_channel_id}>")
        if entry.draft_id:
            lines.append(f"Linked draft: `{entry.draft_id}`")
        if entry.event_plan_id:
            lines.append(f"Linked event plan: `{entry.event_plan_id}`")
        job = jobs.get(entry.entry_id)
        if job is not None:
            lines.append(
                f"Scheduled job: `#{job.job_id}` · {job.kind.value} · {job.status} "
                f"at {job.run_at}"
            )
        elif entry.scheduled_job_id:
            lines.append(f"Scheduled job: `#{entry.scheduled_job_id}`")
        if entry.notes:
            lines.append(f"Notes: {entry.notes}")
        lines.append(f"`entry {entry.entry_id}`")
        embed.add_field(
            name=clip(entry.title, FIELD_NAME_LIMIT),
            value=clip("\n".join(lines), FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(
        text="Entries alone schedule nothing. Linked jobs run read-only analysis or create "
        "drafts; the bot never posts or publishes from the calendar."
    )
    return embed


def reply_draft_embed(reply: ContextualReplyView) -> discord.Embed:
    embed = discord.Embed(
        title=f"Reply draft {reply.reply_id} · {reply.state}",
        colour=BLURPLE,
        description=clip(reply.content, 4096),
    )
    if reply.tone:
        embed.add_field(name="Tone", value=clip(reply.tone, FIELD_VALUE_LIMIT), inline=True)
    if reply.purpose:
        embed.add_field(name="Purpose", value=clip(reply.purpose, FIELD_VALUE_LIMIT), inline=True)
    embed.add_field(name="Created by", value=f"`{reply.created_by}`", inline=True)
    if reply.source_excerpt:
        embed.add_field(
            name="Source excerpt (moderator-supplied)",
            value=clip(reply.source_excerpt, 700),
            inline=False,
        )
    embed.set_footer(
        text="Draft only. The bot will not send it; copy it yourself after reviewing."
    )
    return embed


def reply_list_embed(replies: Sequence[ContextualReplyView]) -> discord.Embed:
    embed = discord.Embed(title="Reply drafts", colour=BLURPLE if replies else GREY)
    if not replies:
        embed.description = "No open reply drafts. Create one with `/reply-draft`."
        return embed
    for reply in replies[:20]:
        excerpt = truncate(neutralize_mentions(reply.content), 200)
        embed.add_field(
            name=clip(f"#{reply.reply_id} · {reply.created_at}", FIELD_NAME_LIMIT),
            value=clip(excerpt, FIELD_VALUE_LIMIT),
            inline=False,
        )
    embed.set_footer(text="View one with /reply-view; drafts are never sent automatically.")
    return embed
