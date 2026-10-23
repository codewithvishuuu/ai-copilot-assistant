"""Regression tests for `/approve-solution` (ModerationCog.approve_solution).

Why this file exists: on 2026-10-19 the same host that lost /reject-solution's
confirmation to an event-loop stall (log: "Can't keep up ... websocket is 15.5s
behind"; the rejection was committed at 10:35:21, its reply POST answered 404 /
10062 "Unknown interaction" only at 10:35:38) also runs this handler. Approve
mutates three things before its only reply (solution state, KB document, audit
row), so the acknowledgment now leaves first - after the auth checks, before the
first state change - exactly like /reject-solution.

Contract pinned here:
- the ack (defer) leaves BEFORE any state change;
- every outcome (approved / not found / already approved) travels through the
  15-minute follow-up window (`reply()` routes to `followup.send` once deferred);
- denials stay direct responses: the ack must not mask the authorization gate.
"""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeInteraction, fake_settings, make_user

from copilot.cogs.moderation import ModerationCog
from copilot.core.solutions import approve_solution, propose_solution
from copilot.db.repository import Repository
from copilot.errors import NotModeratorError


def make_cog(tmp_path, db) -> ModerationCog:
    return ModerationCog(bot=SimpleNamespace(settings=fake_settings(tmp_path), repo=Repository(db)))


def moderator_interaction() -> FakeInteraction:
    return FakeInteraction(user=make_user(7, manage_threads=True))


async def seed_proposed_solution(
    db, *, actor_id: str = "9", guild_id: str = "500", discord_thread_id: str = "1001"
) -> int:
    repo = Repository(db)
    thread_id = await repo.upsert_thread(
        discord_thread_id=discord_thread_id,
        guild_id=guild_id,
        parent_channel_id="42",
        name="Login is broken",
    )
    return await propose_solution(
        repo,
        thread_row_id=thread_id,
        body="Clear the cookies.",
        source_url=None,
        actor_id=actor_id,
    )


async def test_approve_acks_before_the_state_change_and_replies_by_followup(tmp_path, tmp_db):
    solution_id = await seed_proposed_solution(tmp_db)
    repo = Repository(tmp_db)
    interaction = moderator_interaction()
    cog = make_cog(tmp_path, tmp_db)

    # Record the solution state at the moment the ack is sent.
    states_at_ack: list[str] = []
    original_defer = interaction.response.defer

    async def spy_defer(*, ephemeral=False):
        states_at_ack.append((await repo.get_solution(solution_id))["state"])
        await original_defer(ephemeral=ephemeral)

    interaction.response.defer = spy_defer

    await ModerationCog.approve_solution.callback(cog, interaction, solution_id)

    # The ack left while the solution was still 'proposed' - the fix's whole point.
    assert states_at_ack == ["proposed"]
    assert interaction.response.deferred is True
    assert interaction.response.sent == []
    assert [m["content"] for m in interaction.followup.sent] == [
        f"Solution `{solution_id}` approved and added to the knowledge base."
    ]
    assert interaction.followup.sent[0]["ephemeral"] is True

    row = await repo.get_solution(solution_id)
    assert row["state"] == "approved"
    assert row["decided_by"] == "7"
    audits = await tmp_db.fetchall("SELECT id FROM audit_log WHERE action = 'solution.approved'")
    assert len(audits) == 1
    kb = await tmp_db.fetchall(
        "SELECT id FROM kb_docs WHERE kind = 'solution' AND ref_id = ?", (solution_id,)
    )
    assert len(kb) == 1


async def test_approve_unknown_id_acks_then_reports_not_found(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()

    await ModerationCog.approve_solution.callback(cog, interaction, 99)

    assert interaction.response.deferred is True
    assert [m["content"] for m in interaction.followup.sent] == [
        "No proposed solution with id `99`."
    ]


async def test_approve_already_approved_acks_then_reports(tmp_path, tmp_db):
    solution_id = await seed_proposed_solution(tmp_db)
    await approve_solution(
        Repository(tmp_db), solution_id=solution_id, guild_id="500", actor_id="9"
    )
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()

    await ModerationCog.approve_solution.callback(cog, interaction, solution_id)

    assert interaction.response.deferred is True
    assert [m["content"] for m in interaction.followup.sent] == [
        f"Solution `{solution_id}` was already approved."
    ]


async def test_approve_cross_guild_solution_is_reported_as_not_found(tmp_path, tmp_db):
    solution_id = await seed_proposed_solution(
        tmp_db, guild_id="999", discord_thread_id="9991"
    )
    repo = Repository(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()  # guild 500

    await ModerationCog.approve_solution.callback(cog, interaction, solution_id)

    assert interaction.response.deferred is True
    assert [m["content"] for m in interaction.followup.sent] == [
        f"No proposed solution with id `{solution_id}`."
    ]
    assert (await repo.get_solution(solution_id))["state"] == "proposed"
    assert (await tmp_db.counts())["kb_docs"] == 0


async def test_approve_denied_user_is_answered_without_ack_or_state_change(tmp_path, tmp_db):
    solution_id = await seed_proposed_solution(tmp_db)
    repo = Repository(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no moderator permission

    await ModerationCog.approve_solution.callback(cog, interaction, solution_id)

    # Denials stay direct responses: the ack must not mask the authorization gate.
    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    assert interaction.followup.sent == []
    assert (await repo.get_solution(solution_id))["state"] == "proposed"
    audits = await tmp_db.fetchall("SELECT id FROM audit_log WHERE action = 'solution.approved'")
    assert audits == []
