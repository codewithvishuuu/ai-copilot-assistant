"""Insights engine: minimum-data gate and aggregates over stored summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from copilot.core.insights_engine import compute_insights
from copilot.db.repository import Repository


def _row(thread_id, *, status="unresolved", signatures="[]", analysis="{}", row_id=0):
    return {
        "id": row_id or thread_id,
        "thread_id": thread_id,
        "created_at": "2026-10-18T12:00:00+00:00",
        "status": status,
        "error_signatures": signatures,
        "analysis_json": analysis,
    }


class FakeRepo:
    def __init__(self, rows):
        self.rows = rows
        self.since: str | None = None
        self.guild_id: str | None = None

    async def summaries_since(self, since_iso, *, guild_id, limit=1000):
        self.since = since_iso
        self.guild_id = guild_id
        return self.rows


async def test_gate_blocks_below_minimum_distinct_threads():
    repo = FakeRepo([_row(1), _row(1, row_id=2), _row(2)])
    result = await compute_insights(repo, guild_id="500", window_days=30, minimum_threads=3)
    assert result.sufficient is False
    assert result.total_threads == 2  # duplicate summaries of one thread count once
    assert result.status_counts == {}
    assert result.top_errors == []
    assert result.threads_with_open_questions == 0
    assert repo.guild_id == "500"


async def test_aggregates_statuses_errors_and_open_questions():
    repo = FakeRepo(
        [
            _row(
                1,
                status="resolved",
                signatures='["boom", "boom"]',
                analysis='{"unanswered_questions": ["still no answer?"]}',
            ),
            _row(2, status="unresolved", signatures='["boom", "other"]', analysis='{"unanswered_questions": []}'),
            _row(3, status="unresolved", signatures="[]", analysis="{}"),
        ]
    )
    result = await compute_insights(repo, guild_id="500", window_days=30, minimum_threads=3)
    assert result.sufficient is True
    assert result.total_threads == 3
    assert result.status_counts == {"unresolved": 2, "resolved": 1}
    assert result.top_errors == [("boom", 3), ("other", 1)]
    assert result.threads_with_open_questions == 1


async def test_malformed_json_and_odd_values_are_tolerated():
    repo = FakeRepo(
        [
            _row(1, signatures="not json", analysis="{"),
            _row(2),
            _row(3),
        ]
    )
    result = await compute_insights(repo, guild_id="500", window_days=30, minimum_threads=3)
    assert result.sufficient is True
    assert result.status_counts == {"unresolved": 3}
    assert result.top_errors == []
    assert result.threads_with_open_questions == 0


async def test_error_signatures_are_cleaned_and_top_five_kept():
    long_signature = "E" * 200
    rows = [
        _row(1, signatures=json.dumps([long_signature, "   ", 42, "kept"])),
        _row(2, signatures=json.dumps([f"sig-{n}" for n in range(7)])),
        _row(3),
    ]
    result = await compute_insights(FakeRepo(rows), guild_id="500", window_days=30, minimum_threads=3)
    flattened = dict(result.top_errors)
    assert len(result.top_errors) == 5
    assert all(signature.strip() for signature in flattened)
    assert "kept" in flattened
    assert any(signature == "sig-0" for signature in flattened)
    long_entry = next(signature for signature in flattened if signature.startswith("E"))
    assert len(long_entry) == 120


async def test_window_start_is_derived_from_window_days():
    repo = FakeRepo([_row(1), _row(2), _row(3)])
    await compute_insights(repo, guild_id="500", window_days=7, minimum_threads=1)
    parsed = datetime.fromisoformat(repo.since)
    delta_days = (datetime.now(UTC) - parsed).total_seconds() / 86400
    assert 6.9 < delta_days <= 7.0001


async def test_end_to_end_over_stored_summaries(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        thread_id = await repo.upsert_thread(
            discord_thread_id=str(1000 + index), guild_id="500", parent_channel_id=None, name=f"t{index}"
        )
        await repo.insert_summary(
            thread_id=thread_id,
            requested_by="7",
            model="m",
            prompt_version="v1",
            status="resolved" if index == 0 else "unresolved",
            confidence=0.5,
            analysis_json='{"unanswered_questions": ["q"]}' if index == 1 else "{}",
            error_signatures=["boom"] if index < 2 else [],
        )
    other_thread_id = await repo.upsert_thread(
        discord_thread_id="9991", guild_id="999", parent_channel_id=None, name="other guild"
    )
    await repo.insert_summary(
        thread_id=other_thread_id,
        requested_by="8",
        model="m",
        prompt_version="v1",
        status="resolved",
        confidence=0.5,
        analysis_json="{}",
        error_signatures=["other-guild-error"],
    )
    result = await compute_insights(repo, guild_id="500", window_days=30, minimum_threads=3)
    assert result.sufficient is True
    assert result.total_threads == 3
    assert result.status_counts == {"resolved": 1, "unresolved": 2}
    assert result.top_errors == [("boom", 2)]
    assert result.threads_with_open_questions == 1
