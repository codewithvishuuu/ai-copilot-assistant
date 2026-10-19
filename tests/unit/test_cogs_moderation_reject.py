"""Regression tests for `/reject-solution` (ModerationCog.reject_solution).

Live incident 2026-10-19 (data/logs/copilot.log):
    first attempt -> "The application did not respond"; the retry answered
    "Solution 2 was already rejected." Log: 404 Not Found (error code: 10062)
    Unknown interaction. Forensics: the rejection HAD been persisted
    (solutions.decided_at + audit created_at = 10:35:21 local), but the reply
    POST produced its 404 only at 10:35:38.839 — ~17.8 s after the state change,
    past Discord's 3-second initial-response window. The confirmation was lost
    while the state change stuck, and the retry still reported the truth.

Contract pinned here:
- the acknowledgment (defer) leaves BEFORE any state change, so the reply can
  no longer race Discord's 3-second deadline (it travels through the 15-minute
  follow-up window — `reply()` already routes to `followup.send`);
- a lost reply never rolls back or crashes the command: the Discord error
  detail is logged, state stays committed, and a later invocation reports
  "already rejected" idempotently.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
from fakes import FakeFollowup, FakeInteraction, fake_settings, make_user

from copilot.cogs.moderation import ModerationCog
from copilot.core.solutions import propose_solution
from copilot.db.repository import Repository
from copilot.errors import NotModeratorError


def make_cog(tmp_path, db) -> ModerationCog:
    return ModerationCog(bot=SimpleNamespace(settings=fake_settings(tmp_path), repo=Repository(db)))


def moderator_interaction() -> FakeInteraction:
    return FakeInteraction(user=make_user(7, manage_threads=True))


async def seed_proposed_solution(db, *, actor_id: str = "9") -> int:
    repo = Repository(db)
    thread_id = await repo.upsert_thread(
        discord_thread_id="1001", guild_id="500", parent_channel_id="42", name="Login is broken"
    )
    return await propose_solution(
        repo,
        thread_row_id=thread_id,
        body="Clear the cookies.",
        source_url=None,
        actor_id=actor_id,
    )


async def test_reject_acks_before_the_state_change_and_replies_by_followup(tmp_path, tmp_db):
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

    await ModerationCog.reject_solution.callback(cog, interaction, solution_id)

    # The ack left while the solution was still 'proposed' — the fix's whole point.
    assert states_at_ack == ["proposed"]
    assert interaction.response.deferred is True  # ephemeral defer
    assert interaction.response.sent == []
    assert [m["content"] for m in interaction.followup.sent] == [
        f"Solution `{solution_id}` rejected. It stays stored but is not searchable."
    ]
    assert interaction.followup.sent[0]["ephemeral"] is True

    row = await repo.get_solution(solution_id)
    assert row["state"] == "rejected"
    assert row["decided_by"] == "7"
    audits = await tmp_db.fetchall("SELECT action FROM audit_log WHERE action = 'solution.rejected'")
    assert len(audits) == 1


class _LostDeliveryFollowup(FakeFollowup):
    """Follow-up send failing exactly like live (404/10062 Unknown interaction)."""

    async def send(self, *args, **kwargs):
        raise discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10062, "message": "Unknown interaction"},
        )


async def test_lost_reply_keeps_state_and_the_retry_reports_already_rejected(
    tmp_path, tmp_db, caplog
):
    solution_id = await seed_proposed_solution(tmp_db)
    repo = Repository(tmp_db)
    cog = make_cog(tmp_path, tmp_db)

    first = moderator_interaction()
    first.followup = _LostDeliveryFollowup()

    with caplog.at_level(logging.WARNING, logger="copilot.cogs.base"):
        await ModerationCog.reject_solution.callback(cog, first, solution_id)  # must not raise

    # Delivery was lost, but the rejection is committed — exactly like live.
    assert (await repo.get_solution(solution_id))["state"] == "rejected"
    # ...and the failure is logged with its Discord detail, never silently.
    assert "Could not deliver interaction reply" in caplog.text
    assert "10062" in caplog.text
    assert "Unknown interaction" in caplog.text

    # The retry — the message the user actually saw — stays truthful and inert.
    second = moderator_interaction()
    await ModerationCog.reject_solution.callback(cog, second, solution_id)
    assert [m["content"] for m in second.followup.sent] == [
        f"Solution `{solution_id}` was already rejected."
    ]
    audits = await tmp_db.fetchall("SELECT action FROM audit_log WHERE action = 'solution.rejected'")
    assert len(audits) == 1  # the retry changed nothing


async def test_reject_unknown_id_acks_then_reports_not_found(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db)
    interaction = moderator_interaction()

    await ModerationCog.reject_solution.callback(cog, interaction, 99)

    assert interaction.response.deferred is True
    assert [m["content"] for m in interaction.followup.sent] == [
        "No proposed solution with id `99`."
    ]


async def test_reject_denied_user_is_answered_without_ack_or_state_change(tmp_path, tmp_db):
    solution_id = await seed_proposed_solution(tmp_db)
    repo = Repository(tmp_db)
    cog = make_cog(tmp_path, tmp_db)
    interaction = FakeInteraction()  # no moderator permission

    await ModerationCog.reject_solution.callback(cog, interaction, solution_id)

    # Denials stay direct responses: the ack must not mask the authorization gate.
    assert interaction.response.deferred is None
    assert interaction.response.sent[-1]["content"] == str(NotModeratorError())
    assert (await repo.get_solution(solution_id))["state"] == "proposed"
