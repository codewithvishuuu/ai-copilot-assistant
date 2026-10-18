"""Solution workflow: propose -> approve/reject, knowledge-base promotion, audits."""

from __future__ import annotations

from copilot.core.solutions import approve_solution, propose_solution, reject_solution, thread_url
from copilot.db.repository import Repository


def test_thread_url_builds_discord_link():
    assert thread_url({"guild_id": "500", "discord_thread_id": "1001"}) == (
        "https://discord.com/channels/500/1001"
    )
    assert thread_url(None) is None
    assert thread_url({"guild_id": None, "discord_thread_id": "1"}) is None
    assert thread_url({"guild_id": "1", "discord_thread_id": None}) is None


async def _seed_thread(repo, *, discord_id="1001", name="Login is broken"):
    return await repo.upsert_thread(
        discord_thread_id=discord_id, guild_id="500", parent_channel_id="42", name=name
    )


async def test_propose_creates_proposed_solution_and_audit(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    solution_id = await propose_solution(
        repo, thread_row_id=thread_id, body="Clear the cookies.", source_url=None, actor_id="9"
    )
    row = await repo.get_solution(solution_id)
    assert row["state"] == "proposed"
    assert row["body"] == "Clear the cookies."
    assert row["proposed_by"] == "9"
    assert row["decided_by"] is None

    audits = await tmp_db.fetchall("SELECT * FROM audit_log WHERE action = 'solution.proposed'")
    assert len(audits) == 1
    assert audits[0]["target_id"] == str(solution_id)
    assert audits[0]["actor_id"] == "9"


async def test_approve_promotes_to_searchable_knowledge_base(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    solution_id = await propose_solution(
        repo, thread_row_id=thread_id, body="Enable WAL mode.", source_url=None, actor_id="9"
    )

    ok, outcome = await approve_solution(repo, solution_id=solution_id, actor_id="7")
    assert (ok, outcome) == (True, "approved")

    row = await repo.get_solution(solution_id)
    assert row["state"] == "approved"
    assert row["decided_by"] == "7"
    assert row["decided_at"]

    doc = await tmp_db.fetchone(
        "SELECT * FROM kb_docs WHERE kind = 'solution' AND ref_id = ?", (solution_id,)
    )
    assert doc is not None
    assert doc["title"] == "Solution: Login is broken"
    assert doc["body"] == "Enable WAL mode."
    assert doc["url"] == "https://discord.com/channels/500/1001"

    hits = await repo.search_kb("enable WAL mode")
    assert any(hit["id"] == doc["id"] for hit in hits)

    audits = await tmp_db.fetchall("SELECT * FROM audit_log WHERE action = 'solution.approved'")
    assert len(audits) == 1

    assert await approve_solution(repo, solution_id=solution_id, actor_id="7") == (
        False,
        "already_approved",
    )


async def test_approve_prefers_explicit_source_url(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    solution_id = await propose_solution(
        repo,
        thread_row_id=thread_id,
        body="See upstream fix.",
        source_url="https://example.com/fix",
        actor_id="9",
    )
    await approve_solution(repo, solution_id=solution_id, actor_id="7")
    doc = await tmp_db.fetchone(
        "SELECT url FROM kb_docs WHERE kind = 'solution' AND ref_id = ?", (solution_id,)
    )
    assert doc["url"] == "https://example.com/fix"


async def test_approve_title_falls_back_when_thread_has_no_name(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await repo.upsert_thread(
        discord_thread_id="2002", guild_id="500", parent_channel_id=None, name=""
    )
    solution_id = await propose_solution(
        repo, thread_row_id=thread_id, body="fix", source_url=None, actor_id="9"
    )
    await approve_solution(repo, solution_id=solution_id, actor_id="7")
    doc = await tmp_db.fetchone(
        "SELECT title FROM kb_docs WHERE kind = 'solution' AND ref_id = ?", (solution_id,)
    )
    assert doc["title"] == "Solution"


async def test_reject_never_creates_a_kb_doc(tmp_db):
    repo = Repository(tmp_db)
    thread_id = await _seed_thread(repo)
    solution_id = await propose_solution(
        repo, thread_row_id=thread_id, body="Bad idea.", source_url=None, actor_id="9"
    )

    ok, outcome = await reject_solution(repo, solution_id=solution_id, actor_id="7", note="did not work")
    assert (ok, outcome) == (True, "rejected")

    row = await repo.get_solution(solution_id)
    assert row["state"] == "rejected"
    assert row["decided_by"] == "7"
    assert row["note"] == "did not work"
    assert (await tmp_db.counts())["kb_docs"] == 0

    assert await approve_solution(repo, solution_id=solution_id, actor_id="7") == (
        False,
        "already_rejected",
    )
    assert await reject_solution(repo, solution_id=solution_id, actor_id="7") == (
        False,
        "already_rejected",
    )


async def test_decisions_on_missing_solution_report_not_found(tmp_db):
    repo = Repository(tmp_db)
    assert await approve_solution(repo, solution_id=12345, actor_id="7") == (False, "not_found")
    assert await reject_solution(repo, solution_id=12345, actor_id="7") == (False, "not_found")
