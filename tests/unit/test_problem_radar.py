"""Problem Radar: read-model promotion, evidence, uncertainty and guild isolation."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from copilot.core.problem_radar import compute_problem_radar
from copilot.db.repository import Repository

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


async def _seed(
    repo: Repository,
    *,
    guild_id: str = "500",
    index: int,
    questions: list[str] | None = None,
    errors: list[str] | None = None,
) -> None:
    thread_id = await repo.upsert_thread(
        discord_thread_id=f"{guild_id}-{index}",
        guild_id=guild_id,
        parent_channel_id="42",
        name=f"Login issue {index}",
    )
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="test-model",
        prompt_version="test-v1",
        status="unresolved",
        confidence=0.5,
        analysis_json=json.dumps(
            {"unanswered_questions": questions or [], "attempted_solutions": []}
        ),
        error_signatures=errors or [],
    )


async def _radar(repo: Repository, **overrides):
    values = {
        "guild_id": "500",
        "window_days": 30,
        "minimum_threads": 3,
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return await compute_problem_radar(repo, **values)


async def test_below_threshold_returns_an_explicit_empty_state(tmp_db):
    repo = Repository(tmp_db)
    await _seed(repo, index=1, questions=["How do I reset my password?"])

    snapshot = await _radar(repo)

    assert snapshot.sufficient is False
    assert snapshot.total_threads == 1
    assert snapshot.problems == []
    assert snapshot.gaps == []
    assert "No Radar evidence" in snapshot.coverage_summary
    assert snapshot.notes


async def test_repeated_question_becomes_an_evidenced_problem_and_gap(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed(repo, index=index, questions=["How do I reset my password?"])

    snapshot = await _radar(repo)

    assert snapshot.sufficient is True
    assert snapshot.total_threads == 3
    problem = next(item for item in snapshot.problems if item.kind == "repeated_question")
    assert problem.count == 3
    assert problem.confidence == "medium"
    assert problem.kb_status == "missing"
    assert len(problem.evidence) == 3
    assert all(item.url and item.url.startswith("https://discord.com/channels/500/") for item in problem.evidence)
    assert "does not prove" in problem.uncertainty
    second = await _radar(repo)
    assert [item.problem_id for item in second.problems] == [
        item.problem_id for item in snapshot.problems
    ]

    gap = next(item for item in snapshot.gaps if item.title == problem.title)
    assert gap.count == 3
    assert gap.confidence == "medium"
    assert gap.evidence

    assert snapshot.recommendations
    assert all(rec.uncertainty for rec in snapshot.recommendations)
    serialized = snapshot.model_dump_json().lower()
    assert "author" not in serialized


async def test_approved_knowledge_clears_the_gap_but_not_the_observation(tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await _seed(repo, index=index, questions=["How do I reset my password?"])

    before = await _radar(repo)
    assert before.gaps

    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Password reset",
        body="How do I reset my password? Use the reset form.",
        url=None,
        approved=True,
    )

    after = await _radar(repo)
    repeated = [item for item in after.problems if item.kind == "repeated_question"]
    assert repeated
    assert repeated[0].kb_status == "not_checked"
    assert after.gaps == []
    assert "No uncovered recurring topic" in after.coverage_summary


async def test_counts_drive_confidence(tmp_db):
    repo = Repository(tmp_db)
    for index in range(5):
        await _seed(repo, index=index, errors=["ECONNREFUSED redis:6379"])

    snapshot = await _radar(repo)

    recurring = next(item for item in snapshot.problems if item.kind == "recurring_problem")
    assert recurring.count == 5
    assert recurring.confidence == "high"
    assert "different causes" in recurring.uncertainty


async def test_radar_never_crosses_guild_boundaries(tmp_db):
    repo = Repository(tmp_db)
    for index in range(2):
        await _seed(repo, guild_id="500", index=index, questions=["Only guild A?"])
    for index in range(3):
        await _seed(repo, guild_id="999", index=index, questions=["Only guild B?"])

    guild_a = await _radar(repo, guild_id="500")
    guild_b = await _radar(repo, guild_id="999")

    assert guild_a.sufficient is False
    assert guild_b.sufficient is True
    refs = {
        item.ref_id
        for problem in guild_b.problems
        for item in problem.evidence
    }
    refs |= {item.ref_id for gap in guild_b.gaps for item in gap.evidence}
    assert refs
    assert all(ref.startswith("999-") for ref in refs)
