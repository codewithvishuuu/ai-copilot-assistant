"""Embed builders: Discord limits, paging, mention neutralization, honest wording."""

from __future__ import annotations

from copilot.core.analyzer import AnalysisResult, DebugResult
from copilot.core.collector import CollectedMessage, CollectedThread
from copilot.core.duplicate import RelatedDoc
from copilot.core.health import DatabaseSection, HealthReport, OllamaSection, QueueSection
from copilot.core.insights_engine import InsightsResult
from copilot.llm.schemas import DebugAnalysis, ThreadAnalysis
from copilot.ui.embeds import (
    GREEN,
    MAX_FIELDS,
    ORANGE,
    RED,
    STATUS_COLORS,
    _PagedEmbeds,
    build_debug_embed,
    build_insights_embed,
    build_kb_search_embeds,
    build_status_embeds,
    build_summary_embeds,
    clip,
    message_link,
)


def _thread(messages=None, *, name="Login flow is broken"):
    if messages is None:
        messages = [
            CollectedMessage(
                id="1001",
                author="maya",
                created_at="2026-10-18T12:00:00+00:00",
                content="opening",
                is_opening=True,
            ),
            CollectedMessage(
                id="1002", author="kevin", created_at="2026-10-18T12:05:00+00:00", content="reply"
            ),
        ]
    return CollectedThread(
        thread_id="1001",
        guild_id="500",
        parent_channel_id="42",
        thread_name=name,
        jump_url="https://discord.com/channels/500/1001",
        messages=messages,
    )


def _analysis(**overrides):
    payload = {
        "overview": "Users cannot log in after the upgrade.",
        "main_problem": "Refresh token rejected.",
        "key_findings": [{"text": "Only SSO accounts affected.", "source_message_ids": ["1001", "1002"]}],
        "current_status": "unresolved",
        "status_confidence": 0.4,
        "suggested_next_step": "Capture the token exchange.",
    }
    payload.update(overrides)
    return ThreadAnalysis.model_validate(payload)


def _result(**overrides):
    kwargs = {
        "analysis": _analysis(),
        "collected": _thread(),
        "model": "gemma4:e2b",
        "prompt_version": "analysis-v1",
        "elapsed_s": 12.3,
    }
    kwargs.update(overrides)
    return AnalysisResult(**kwargs)


def _health_report(**overrides):
    ollama = overrides.pop(
        "ollama",
        OllamaSection(
            reachable=True,
            version="9.9.9",
            model="gemma4:e2b",
            model_exists=True,
            supports_vision=True,
            context_length=131072,
        ),
    )
    database = overrides.pop(
        "database",
        DatabaseSection(
            ok=True,
            path="C:/data/copilot.db",
            user_version=2,
            counts={"threads": 4, "summaries": 9, "kb_docs": 3},
        ),
    )
    queue = overrides.pop(
        "queue",
        QueueSection(pending=0, capacity=3, active=False, processed_total=10, cancelled_total=1, failed_total=2),
    )
    base = dict(
        ollama=ollama,
        database=database,
        queue=queue,
        bot_latency_s=0.123,
        guild_count=2,
        version="0.1.0",
    )
    base.update(overrides)
    return HealthReport(**base)


def _field(embed, needle):
    for field in embed.fields:
        if needle in field.name:
            return field
    raise AssertionError(f"no field containing {needle!r}; have {[f.name for f in embed.fields]}")


# ------------------------------------------------------------------ primitives


def test_clip_neutralizes_mentions_and_bounds_length():
    text = clip("@everyone " + "x" * 300, 50)
    assert "@\u200beveryone" in text
    assert "@everyone" not in text
    assert len(text) <= 50
    assert text.endswith("…")


def test_message_link_format():
    assert message_link("500", "1001", "99") == "https://discord.com/channels/500/1001/99"


# --------------------------------------------------------------------- summary


def test_summary_embed_contains_expected_sections():
    embeds = build_summary_embeds(_result())
    assert len(embeds) == 1
    embed = embeds[0]

    assert embed.title == "Summary — Login flow is broken"
    assert embed.url == "https://discord.com/channels/500/1001"
    assert embed.colour.value == STATUS_COLORS["unresolved"]
    assert embed.description == "Users cannot log in after the upgrade."

    assert _field(embed, "Main problem").value == "Refresh token rejected."
    status = _field(embed, "Status")
    assert "AI suggestion" in status.name
    assert "Unresolved" in status.value and "40% confidence" in status.value
    assert _field(embed, "Suggested next step").value == "Capture the token exchange."

    findings = _field(embed, "Key findings").value
    assert "- Only SSO accounts affected." in findings
    assert "[1](https://discord.com/channels/500/1001/1001)" in findings
    assert "[2](https://discord.com/channels/500/1001/1002)" in findings

    sources = _field(embed, "Sources").value
    assert "[maya · 2026-10-18 12:00](https://discord.com/channels/500/1001/1001)" in sources
    assert "[kevin · 2026-10-18 12:05](https://discord.com/channels/500/1001/1002)" in sources

    assert embed.footer.text.startswith("AI-generated — verify before acting")
    assert "prompt analysis-v1" in embed.footer.text
    assert "repair retry" not in embed.footer.text


def test_summary_embed_caps_evidence_links_at_three_per_item():
    result = _result(
        analysis=_analysis(
            key_findings=[
                {
                    "text": "many refs",
                    "source_message_ids": ["1001", "1002", "1003", "1004"],
                }
            ]
        )
    )
    findings = _field(build_summary_embeds(result)[0], "Key findings").value
    assert "[3](" in findings
    assert "[4](" not in findings


def test_summary_embed_neutralizes_mentions_and_truncates_long_text():
    result = _result(analysis=_analysis(overview="@everyone " + "x" * 5000))
    embed = build_summary_embeds(result)[0]
    assert "@\u200beveryone" in embed.description
    assert "@everyone" not in embed.description
    assert len(embed.description) <= 4096


def test_summary_embed_shows_correction_related_and_notes():
    related = [
        RelatedDoc(kind="solution", title="Use WAL", url="https://kb.invalid/1", snippet="matched text", score=-2.0),
        RelatedDoc(kind="thread", title="Older report", url=None, snippet="same error", score=-1.0),
    ]
    correction = {
        "status": "resolved",
        "note": "OP confirmed the fix",
        "actor_id": "9",
        "created_at": "2026-10-18T14:00:00+00:00",
    }
    result = _result(
        warnings=[f"w-{n}" for n in range(12)],
        images_analyzed=2,
        repair_used=True,
    )

    embed = build_summary_embeds(result, correction=correction, related=related)[0]

    correction_value = _field(embed, "Moderator correction").value
    assert "**Resolved**" in correction_value
    assert "<@9>" in correction_value
    assert "2026-10-18T14:00" in correction_value
    assert "OP confirmed the fix" in correction_value

    related_value = _field(embed, "Possibly related").value
    assert "**[Use WAL](https://kb.invalid/1)** (solution)" in related_value
    assert "**Older report** (thread)" in related_value
    assert "not verified" in _field(embed, "Possibly related").name

    notes = _field(embed, "Notes").value
    assert "- w-0" in notes
    assert "- w-9" in notes
    assert "w-10" not in notes and "w-11" not in notes  # only the first 10 warnings
    assert "- 2 image(s) were included as model input" in notes

    assert embed.footer.text.endswith("1 repair retry")


# ---------------------------------------------------------------------- paging


def test_paged_embeds_start_a_new_embed_after_twenty_five_fields():
    page = _PagedEmbeds(title="t", description="d", url=None, color=0x123456)
    for index in range(26):
        page.add_field(f"name {index}", f"value {index}")
    embeds = page.build()
    assert len(embeds) == 2
    assert len(embeds[0].fields) == MAX_FIELDS
    assert len(embeds[1].fields) == 1
    assert embeds[1].fields[0].name == "name 25"


def test_paged_embeds_respect_the_visible_text_budget():
    page = _PagedEmbeds(title="t", description="d", url=None, color=0x123456)
    for index in range(7):
        page.add_field(f"n{index}", "v" * 1000)
    embeds = page.build()
    assert len(embeds[0].fields) == 5  # 6th field would exceed the 5500-char budget
    assert len(embeds) == 2
    assert len(embeds[1].fields) == 2


# ---------------------------------------------------------------------- status


def test_status_embed_reports_every_healthy_subsystem():
    embed = build_status_embeds(_health_report(), allowlisted=True, cooldown_s=0)[0]
    assert embed.colour.value == GREEN

    discord_field = _field(embed, "Discord").value
    assert "gateway latency: 123 ms" in discord_field
    assert "✅ this server is allowlisted" in discord_field

    model_field = _field(embed, "Local model").value
    assert "server reachable (v9.9.9)" in model_field
    assert "`gemma4:e2b` installed" in model_field
    assert "image analysis: supported ✅" in model_field
    assert "context window: 131,072 tokens" in model_field

    db_field = _field(embed, "Database").value
    assert "SQLite OK · schema v2" in db_field
    assert "4 thread(s), 9 summary(ies), 3 KB doc(s)" in db_field
    assert "copilot.db" in db_field

    queue_field = _field(embed, "Inference queue").value
    assert "pending: 0/3" in queue_field
    assert "active job: no" in queue_field
    assert "done 10 · cancelled 1 · failed 2" in queue_field
    assert "your cooldown" not in queue_field

    assert embed.footer.text.startswith("v0.1.0 · checks ran at")


def test_status_embed_unreachable_server_is_red_and_honest():
    report = _health_report(
        ollama=OllamaSection(
            reachable=False,
            version=None,
            model="",
            model_exists=False,
            supports_vision=False,
            context_length=None,
            detail="connection refused",
        )
    )
    embed = build_status_embeds(report, allowlisted=False, cooldown_s=0)[0]
    assert embed.colour.value == RED
    model_field = _field(embed, "Local model").value
    assert "server not reachable" in model_field
    assert "connection refused" in model_field
    assert "⚠️ this server is not allowlisted" in _field(embed, "Discord").value


def test_status_embed_missing_model_shows_pull_hint():
    report = _health_report(
        ollama=OllamaSection(
            reachable=True,
            version="0.40.2",
            model="gemma4:e2b",
            model_exists=False,
            supports_vision=False,
            context_length=None,
            detail="model not installed",
        )
    )
    embed = build_status_embeds(report, allowlisted=True, cooldown_s=0)[0]
    assert embed.colour.value == ORANGE
    model_field = _field(embed, "Local model").value
    assert "NOT installed" in model_field
    assert "ollama pull gemma4:e2b" in model_field


def test_status_embed_database_problem_and_active_cooldown():
    report = _health_report(
        database=DatabaseSection(ok=False, path="p.db", user_version=0, counts={}, error="ping failed"),
        queue=QueueSection(
            pending=1,
            capacity=3,
            active=True,
            processed_total=0,
            cancelled_total=0,
            failed_total=0,
            cooldown_remaining_s=41.4,
        ),
    )
    embed = build_status_embeds(report, allowlisted=True, cooldown_s=30)[0]
    assert embed.colour.value == ORANGE
    assert "❌ problem: ping failed" in _field(embed, "Database").value
    queue_field = _field(embed, "Inference queue").value
    assert "active job: yes" in queue_field
    assert "your cooldown: 41s" in queue_field


# ------------------------------------------------------------------ kb search


def test_kb_search_embed_formats_solution_and_thread_rows():
    rows = [
        {
            "id": 7,
            "kind": "solution",
            "title": "Use WAL",
            "snippet": "enable [WAL] mode",
            "body": "full body",
            "url": "https://kb.invalid/1",
            "approved": 1,
        },
        {
            "id": 8,
            "kind": "thread",
            "title": "Old report",
            "snippet": None,
            "body": "body text here",
            "url": None,
            "approved": 0,
        },
    ]
    embed = build_kb_search_embeds("wal", rows)[0]

    assert embed.title == "🔎 Knowledge base: wal"
    assert "2 result(s)" in embed.description
    assert "moderator-verified" in embed.description
    assert "unreviewed AI summaries" in embed.description

    solution = _field(embed, "Use WAL")
    assert "✅ solution" in solution.name
    assert "enable [WAL] mode" in solution.value
    assert "[Open source](https://kb.invalid/1)" in solution.value
    assert "`doc 7` · approved" in solution.value

    thread = _field(embed, "Old report")
    assert "🧵 thread" in thread.name
    assert "body text here" in thread.value  # falls back to the body when there is no snippet
    assert "`doc 8` · unreviewed" in thread.value


# ------------------------------------------------------------------- insights


def test_insights_embed_shows_the_gate_until_enough_data():
    result = InsightsResult(window_days=30, minimum_threads=5, total_threads=3, sufficient=False)
    embed = build_insights_embed(result)
    assert "**3** of **5**" in embed.description
    assert "last 30 days" in embed.description
    assert "unlock automatically" in embed.description
    assert embed.colour.value == ORANGE
    assert embed.footer.text == "no per-user tracking; aggregates only"


def test_insights_embed_shows_aggregates_once_sufficient():
    result = InsightsResult(
        window_days=7,
        minimum_threads=3,
        total_threads=5,
        sufficient=True,
        status_counts={"resolved": 3, "unresolved": 2},
        top_errors=[("boom", 4), ("oom", 1)],
        threads_with_open_questions=2,
    )
    embed = build_insights_embed(result)

    assert "Based on 5 summarized thread(s) in the last 7 days." in embed.description
    assert _field(embed, "Issue status distribution").value == "Resolved: 3\nUnresolved: 2"
    assert _field(embed, "Most repeated error signatures").value == "1. boom ×4\n2. oom ×1"
    assert "2 summarized thread(s)" in _field(embed, "Open questions").value
    assert "aggregates only" in embed.footer.text


# ---------------------------------------------------------------------- debug


def test_debug_embed_lists_causes_and_steps():
    result = DebugResult(
        analysis=DebugAnalysis(
            likely_causes=["stale token", "@everyone typo in config"],
            next_steps=["re-auth the bot"],
            notes="low confidence",
        ),
        model="gemma4:e2b",
        elapsed_s=3.2,
        repair_used=True,
    )
    embed = build_debug_embed(result)

    assert "verify" in embed.title.lower()
    assert "Nothing was executed, fetched, or verified." in embed.description
    causes = _field(embed, "Likely causes").value
    assert causes.startswith("1. stale token")
    assert "@\u200beveryone" in causes
    assert "1. re-auth the bot" in _field(embed, "Safe next steps").value
    assert _field(embed, "Notes").value == "low confidence"
    assert embed.footer.text.endswith("1 repair retry")


def test_debug_embed_handles_empty_analysis_gracefully():
    result = DebugResult(analysis=DebugAnalysis(), model="m", elapsed_s=1.0)
    embed = build_debug_embed(result)
    assert embed.fields == []
    assert "AI-generated" in embed.footer.text
