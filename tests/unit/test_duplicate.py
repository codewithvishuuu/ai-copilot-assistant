"""Possibly-related detection: FTS-backed RelatedDoc mapping + fingerprint text."""

from __future__ import annotations

from copilot.core.duplicate import RelatedDoc, find_related, fingerprint_text
from copilot.db.repository import Repository
from copilot.llm.schemas import ThreadAnalysis


async def _seed_docs(repo):
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=1,
        guild_id="500",
        title="Enable WAL",
        body="sqlite database locked fix: enable WAL mode",
        url=None,
        approved=True,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=2,
        guild_id="500",
        title="Retry writes",
        body="database locked errors go away when writes are retried with backoff",
        url=None,
        approved=True,
    )
    await repo.insert_kb_doc(
        kind="solution",
        ref_id=3,
        guild_id="500",
        title="Unrelated theming",
        body="css color variables for dark mode",
        url=None,
        approved=True,
    )


async def test_find_related_returns_mapped_documents(tmp_db):
    repo = Repository(tmp_db)
    await _seed_docs(repo)
    related = await find_related(repo, "database locked", guild_id="500")
    assert related
    assert all(isinstance(doc, RelatedDoc) for doc in related)
    assert "Unrelated theming" not in [doc.title for doc in related]

    first = related[0]
    assert isinstance(first.score, float)
    assert "[" in first.snippet
    assert first.url is None
    assert first.kind == "solution"


async def test_find_related_ignores_empty_text(tmp_db):
    repo = Repository(tmp_db)
    await _seed_docs(repo)
    assert await find_related(repo, "", guild_id="500") == []
    assert await find_related(repo, "   \n ", guild_id="500") == []


async def test_find_related_respects_limit(tmp_db):
    repo = Repository(tmp_db)
    for index in range(5):
        await repo.insert_kb_doc(
            kind="solution",
            ref_id=index,
            guild_id="500",
            title=f"Doc {index}",
            body="database locked workaround",
            url=None,
            approved=True,
        )
    assert len(await find_related(repo, "database locked", guild_id="500", limit=2)) == 2


async def test_find_related_excludes_the_source_thread(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await repo.upsert_thread(
        discord_thread_id="777",
        guild_id="500",
        parent_channel_id=None,
        name="Database locked on write",
    )
    await repo.insert_kb_doc(
        kind="thread",
        ref_id=thread_id,
        guild_id="500",
        title="Database locked on write",
        body="database locked when writing from two processes",
        url=None,
        approved=True,
    )
    related = await find_related(
        repo, "database locked", guild_id="500", exclude_discord_thread_id="777"
    )
    assert "Database locked on write" not in [doc.title for doc in related]


def test_fingerprint_text_uses_overview_problem_and_first_three_errors():
    analysis = ThreadAnalysis.model_validate(
        {
            "overview": "Users report login failures.",
            "main_problem": "Refresh token rejected.",
            "errors": [{"text": f"error {n}"} for n in range(5)],
        }
    )
    text = fingerprint_text(analysis)
    assert text.startswith("Users report login failures. Refresh token rejected.")
    assert "error 0" in text and "error 2" in text
    assert "error 3" not in text and "error 4" not in text


def test_fingerprint_text_empty_when_nothing_to_say():
    assert fingerprint_text(ThreadAnalysis.model_validate({})) == ""
