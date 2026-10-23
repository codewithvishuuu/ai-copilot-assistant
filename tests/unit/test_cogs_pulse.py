"""`/pulse` moderator command: threshold honesty and bounded evidence rendering."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fakes import FakeInteraction, fake_settings, make_user

from copilot.cogs.pulse import PulseCog
from copilot.db.repository import Repository


def make_cog(tmp_path, db, **settings_overrides) -> PulseCog:
    return PulseCog(
        bot=SimpleNamespace(
            settings=fake_settings(tmp_path, **settings_overrides),
            repo=Repository(db),
            db=db,
        )
    )


async def seed(repo: Repository, index: int, *, question: str = "How do I reset my password?"):
    thread_id = await repo.upsert_thread(
        discord_thread_id=f"500-{index}",
        guild_id="500",
        parent_channel_id="42",
        name=f"Password reset {index}",
    )
    await repo.insert_summary(
        thread_id=thread_id,
        requested_by="7",
        model="test-model",
        prompt_version="test-v1",
        status="unresolved",
        confidence=0.5,
        analysis_json=json.dumps({"unanswered_questions": [question]}),
        error_signatures=[],
    )


async def test_pulse_reports_insufficient_data_without_inventing_signals(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    await seed(repo, 1)
    await seed(repo, 2)
    cog = make_cog(tmp_path, tmp_db, insights_min_threads=5)
    interaction = FakeInteraction(user=make_user(7, manage_threads=True))

    await PulseCog.pulse.callback(cog, interaction)

    sent = interaction.followup.sent[-1]["content"]
    assert "Not enough summarized threads" in sent
    assert "2 of 5" in sent
    assert "Nothing was inferred" in sent


async def test_pulse_renders_observations_and_labeled_estimates(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    for index in range(3):
        await seed(repo, index)
    cog = make_cog(tmp_path, tmp_db, insights_min_threads=3)
    interaction = FakeInteraction(user=make_user(7, manage_threads=True))

    await PulseCog.pulse.callback(cog, interaction)

    embed = interaction.followup.sent[-1]["embed"]
    rendered = embed.to_dict()
    text = json.dumps(rendered)
    assert "Observations (stored data)" in text
    assert "Estimates are not measured outcomes" in rendered["footer"]["text"]
    assert "no member profiles" in rendered["description"].lower()
    assert len(rendered["fields"]) <= 10


async def test_pulse_denied_for_non_moderator(tmp_path, tmp_db):
    cog = make_cog(tmp_path, tmp_db, insights_min_threads=3)
    interaction = FakeInteraction()

    await PulseCog.pulse.callback(cog, interaction)

    assert "moderator" in interaction.response.sent[-1]["content"].lower()
