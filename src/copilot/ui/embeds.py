"""Discord embed builders — the entire user interface.

All outputs respect Discord's hard limits: title 256, description 4096, field
name 256, field value 1024, 25 fields and ~6000 characters of visible text per
embed (extra fields page into additional embeds). Text is mention-neutralized;
call sites additionally send with ``allowed_mentions=none``.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import discord

from ..core.analyzer import AnalysisResult, DebugResult
from ..core.collector import CollectedThread
from ..core.duplicate import RelatedDoc
from ..core.health import HealthReport
from ..core.insights_engine import InsightsResult
from ..llm.schemas import STATUS_LABELS, EvidenceItem
from ..util.text import neutralize_mentions, truncate

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
MAX_FIELDS = 25
EMBED_TEXT_BUDGET = 5500  # conservative margin under Discord's 6000 chars/embed

STATUS_COLORS = {
    "unresolved": 0xE67E22,
    "in_progress": 0x5865F2,
    "waiting_for_user": 0xFEE75C,
    "resolved": 0x57F287,
    "unclear": 0x95A5A6,
}
GREEN = 0x57F287
ORANGE = 0xE67E22
RED = 0xED4245


def clip(text: str, limit: int) -> str:
    return truncate(neutralize_mentions(text or ""), limit, suffix=" …")


def message_link(guild_id: str | int, channel_id: str | int, message_id: str | int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class _PagedEmbeds:
    """Accumulates fields across embeds, never exceeding Discord's per-embed budget."""

    def __init__(self, *, title: str, description: str, url: str | None, color: int) -> None:
        first = discord.Embed(
            title=clip(title, TITLE_LIMIT),
            description=clip(description, DESCRIPTION_LIMIT),
            colour=color,
        )
        if url:
            first.url = url
        self.embeds: list[discord.Embed] = [first]
        self._used = len(first.title or "") + len(first.description or "")
        self._color = color

    def add_field(self, name: str, value: str, *, inline: bool = False) -> None:
        name = clip(name, FIELD_NAME_LIMIT)
        value = clip(value, FIELD_VALUE_LIMIT) or "—"
        embed = self.embeds[-1]
        if len(embed.fields) >= MAX_FIELDS or (self._used + len(name) + len(value)) > EMBED_TEXT_BUDGET:
            embed = discord.Embed(colour=self._color)
            self.embeds.append(embed)
            self._used = 0
        embed.add_field(name=name, value=value, inline=inline)
        self._used += len(name) + len(value)

    def set_footer(self, text: str) -> None:
        self.embeds[-1].set_footer(text=clip(text, FOOTER_LIMIT))

    def build(self) -> list[discord.Embed]:
        return self.embeds


# ------------------------------------------------------------------- summary


def _evidence_text(items: list[EvidenceItem], thread: CollectedThread) -> str:
    lines: list[str] = []
    for item in items:
        refs = " ".join(
            f"[{index}]({message_link(thread.guild_id, thread.thread_id, mid)})"
            for index, mid in enumerate(item.source_message_ids[:3], start=1)
        )
        line = f"- {neutralize_mentions(item.text)}"
        if refs:
            line += f" — {refs}"
        lines.append(line)
    return "\n".join(lines)


def _sources_text(thread: CollectedThread, limit: int = 8) -> str:
    lines = [
        f"- [{message.author} · {message.created_at[:16].replace('T', ' ')}]"
        f"({message_link(thread.guild_id, thread.thread_id, message.id)})"
        for message in thread.messages[:limit]
    ]
    remaining = len(thread.messages) - limit
    if remaining > 0:
        lines.append(f"…and {remaining} more collected message(s)")
    return "\n".join(lines)


def _related_text(related: list[RelatedDoc]) -> str:
    lines = []
    for doc in related:
        badge = "solution" if doc.kind == "solution" else "thread"
        title = clip(doc.title or "untitled", 80)
        snippet = clip(doc.snippet, 160)
        if doc.url:
            lines.append(f"- **[{title}]({doc.url})** ({badge}) — {snippet}")
        else:
            lines.append(f"- **{title}** ({badge}) — {snippet}")
    return "\n".join(lines)


def _notes_text(result: AnalysisResult) -> str:
    lines = [f"- {neutralize_mentions(note)}" for note in result.warnings[:10]]
    if result.analysis.analysis_notes:
        lines.append(f"- model note: {neutralize_mentions(result.analysis.analysis_notes)}")
    if result.images_analyzed:
        lines.append(f"- {result.images_analyzed} image(s) were included as model input")
    return "\n".join(lines)


def build_summary_embeds(
    result: AnalysisResult,
    *,
    correction: object | None = None,
    related: list[RelatedDoc] | None = None,
) -> list[discord.Embed]:
    analysis = result.analysis
    thread = result.collected
    color = STATUS_COLORS.get(analysis.current_status.value, ORANGE)

    page = _PagedEmbeds(
        title=f"Summary — {thread.thread_name or 'thread'}",
        description=analysis.overview or "(no overview produced)",
        url=thread.jump_url or None,
        color=color,
    )

    page.add_field("🧩 Main problem", analysis.main_problem or "—")
    status_value = f"**{analysis.status_label()}** · {int(analysis.status_confidence * 100)}% confidence"
    if analysis.status_reason:
        status_value += f"\n{analysis.status_reason}"
    page.add_field("🚦 Status (AI suggestion — not authoritative)", status_value)

    if correction is not None:
        label = STATUS_LABELS.get(str(correction["status"]), str(correction["status"]))
        note = f"\n{correction['note']}" if correction["note"] else ""
        page.add_field(
            "🛠 Moderator correction",
            f"**{label}** — set by <@{correction['actor_id']}> on {str(correction['created_at'])[:16]}{note}",
        )

    page.add_field("🧭 Suggested next step", analysis.suggested_next_step or "—")

    if analysis.unanswered_questions:
        page.add_field(
            "❓ Unanswered questions",
            "\n".join(f"- {neutralize_mentions(q)}" for q in analysis.unanswered_questions),
        )

    if analysis.key_findings:
        page.add_field("🔍 Key findings", _evidence_text(analysis.key_findings, thread))
    if analysis.decisions:
        page.add_field("✅ Decisions", _evidence_text(analysis.decisions, thread))
    if analysis.attempted_solutions:
        page.add_field("🧪 Attempted solutions", _evidence_text(analysis.attempted_solutions, thread))
    if analysis.errors:
        page.add_field("🐞 Errors & blockers", _evidence_text(analysis.errors, thread))

    if related:
        page.add_field("🧷 Possibly related (not verified)", _related_text(related))

    if thread.messages:
        page.add_field("🔗 Sources", _sources_text(thread))

    notes = _notes_text(result)
    if notes:
        page.add_field("📝 Notes", notes)

    footer = (
        f"AI-generated — verify before acting · {result.model} · {result.elapsed_s:.0f}s"
        f" · prompt {result.prompt_version}"
    )
    if result.repair_used:
        footer += " · 1 repair retry"
    page.set_footer(footer)
    return page.build()


# -------------------------------------------------------------------- status


def build_status_embeds(
    report: HealthReport, *, allowlisted: bool, cooldown_s: int
) -> list[discord.Embed]:
    if not report.ollama.reachable:
        color = RED
    elif report.ollama.model_exists and report.database.ok:
        color = GREEN
    else:
        color = ORANGE

    latency = (
        f"{report.bot_latency_s * 1000:.0f} ms"
        if report.bot_latency_s == report.bot_latency_s
        else "unknown"
    )
    page = _PagedEmbeds(
        title="🩺 Community Copilot status",
        description="Live checks, run just now. Nothing here is cached success.",
        url=None,
        color=color,
    )
    page.add_field(
        "Discord",
        f"gateway latency: {latency} · servers visible: {report.guild_count}\n"
        f"{'✅ this server is allowlisted' if allowlisted else '⚠️ this server is not allowlisted'}",
    )

    ollama = report.ollama
    model_lines = []
    if ollama.reachable:
        model_lines.append(f"✅ server reachable (v{ollama.version or '?'})")
        if ollama.model_exists:
            model_lines.append(f"✅ model `{ollama.model}` installed")
            model_lines.append(
                f"image analysis: {'supported ✅' if ollama.supports_vision else 'not supported ❌'}"
            )
            if ollama.context_length:
                model_lines.append(f"context window: {ollama.context_length:,} tokens")
        else:
            model_lines.append(
                f"❌ model `{ollama.model}` NOT installed — run: `ollama pull {ollama.model}`"
            )
    else:
        model_lines.append("❌ server not reachable — start Ollama on the host machine")
    if ollama.detail:
        model_lines.append(f"detail: {clip(ollama.detail, 200)}")
    page.add_field("Local model (Ollama)", "\n".join(model_lines))

    db = report.database
    if db.ok:
        threads = db.counts.get("threads", 0)
        summaries = db.counts.get("summaries", 0)
        kb = db.counts.get("kb_docs", 0)
        page.add_field(
            "Database",
            f"✅ SQLite OK · schema v{db.user_version}\n"
            f"{threads} thread(s), {summaries} summary(ies), {kb} KB doc(s) · `{Path(db.path).name}`",
        )
    else:
        page.add_field("Database", f"❌ problem: {clip(db.error or 'unknown', 200)}")

    queue = report.queue
    queue_lines = [
        f"pending: {queue.pending}/{queue.capacity}",
        f"active job: {'yes' if queue.active else 'no'}",
        f"done {queue.processed_total} · cancelled {queue.cancelled_total} · failed {queue.failed_total}",
    ]
    if cooldown_s and queue.cooldown_remaining_s > 0:
        queue_lines.append(f"your cooldown: {queue.cooldown_remaining_s:.0f}s")
    page.add_field("Inference queue", "\n".join(queue_lines))

    from datetime import datetime

    page.set_footer(
        f"v{report.version or '?'} · checks ran at "
        f"{datetime.now(UTC).isoformat(timespec='seconds')}"
    )
    return page.build()


# ------------------------------------------------------------------ kb search


def build_kb_search_embeds(query: str, rows: list) -> list[discord.Embed]:
    page = _PagedEmbeds(
        title=f"🔎 Knowledge base: {query}",
        description=f"{len(rows)} result(s). Approved solutions are moderator-verified; "
        "thread results are AI summaries of past discussions.",
        url=None,
        color=0x5865F2,
    )
    for row in rows:
        badge = "✅ solution" if row["kind"] == "solution" else "🧵 thread"
        name = clip(f"{badge} · {row['title'] or 'untitled'}", FIELD_NAME_LIMIT)
        body = clip(row["snippet"] or row["body"] or "", 700)
        if row["url"]:
            body += f"\n[Open source]({row['url']})"
        page.add_field(name, body)
    return page.build()


# ------------------------------------------------------------------- insights


def build_insights_embed(result: InsightsResult) -> discord.Embed:
    if not result.sufficient:
        embed = discord.Embed(
            title="📊 Community insights",
            description=(
                f"Not enough data yet: **{result.total_threads}** of **{result.minimum_threads}** "
                f"thread(s) summarized in the last {result.window_days} days. "
                "Insights unlock automatically once there is enough history."
            ),
            colour=ORANGE,
        )
        embed.set_footer(text="no per-user tracking; aggregates only")
        return embed

    embed = discord.Embed(
        title="📊 Community insights",
        description=f"Based on {result.total_threads} summarized thread(s) in the last {result.window_days} days.",
        colour=0x5865F2,
    )
    if result.status_counts:
        lines = [
            f"{STATUS_LABELS.get(status, status)}: {count}"
            for status, count in sorted(result.status_counts.items(), key=lambda kv: -kv[1])
        ]
        embed.add_field(name="Issue status distribution", value="\n".join(lines)[:FIELD_VALUE_LIMIT], inline=False)
    if result.top_errors:
        lines = [
            f"{index}. {neutralize_mentions(sig)} ×{count}"
            for index, (sig, count) in enumerate(result.top_errors, start=1)
        ]
        embed.add_field(name="Most repeated error signatures", value="\n".join(lines)[:FIELD_VALUE_LIMIT], inline=False)
    embed.add_field(
        name="Open questions",
        value=f"{result.threads_with_open_questions} summarized thread(s) still had unanswered questions.",
        inline=False,
    )
    embed.set_footer(text="aggregates only · recurring issues are candidates for pinned answers")
    return embed


# ---------------------------------------------------------------------- debug


def build_debug_embed(result: DebugResult) -> discord.Embed:
    embed = discord.Embed(
        title="🧰 Error analysis (AI-assisted — verify)",
        description="This explains the text you submitted. Nothing was executed, fetched, or verified.",
        colour=0x5865F2,
    )
    if result.analysis.likely_causes:
        embed.add_field(
            name="🔍 Likely causes (ranked)",
            value="\n".join(
                f"{index}. {neutralize_mentions(cause)}"
                for index, cause in enumerate(result.analysis.likely_causes, start=1)
            )[:FIELD_VALUE_LIMIT],
            inline=False,
        )
    if result.analysis.next_steps:
        embed.add_field(
            name="🧪 Safe next steps",
            value="\n".join(
                f"{index}. {neutralize_mentions(step)}"
                for index, step in enumerate(result.analysis.next_steps, start=1)
            )[:FIELD_VALUE_LIMIT],
            inline=False,
        )
    if result.analysis.notes:
        embed.add_field(
            name="📝 Notes", value=clip(result.analysis.notes, FIELD_VALUE_LIMIT), inline=False
        )
    footer = f"AI-generated · {result.model} · {result.elapsed_s:.0f}s"
    if result.repair_used:
        footer += " · 1 repair retry"
    embed.set_footer(text=footer)
    return embed
