"""Content planning: calendar entries, event plans and unsent reply drafts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakes import FakeProvider, fake_settings

from copilot.core.approvals import ActorKind, ApprovalActor, ApprovalPermission
from copilot.core.content_planning import (
    CalendarKind,
    CalendarState,
    ContentPlanningService,
    EventPlanView,
)
from copilot.core.content_studio import content_hash
from copilot.db.repository import Repository
from copilot.errors import (
    ApprovalDeniedError,
    ContentPlanError,
    ContextualReplyNotFoundError,
    EventPlanNotFoundError,
)

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
FUTURE = "2026-11-05T18:00:00+00:00"
PAST = "2026-01-05T18:00:00+00:00"
ALL = frozenset(
    {
        ApprovalPermission.propose,
        ApprovalPermission.decide,
        ApprovalPermission.cancel,
        ApprovalPermission.execute,
    }
)


def _actor(guild_id: int = 500, **overrides) -> ApprovalActor:
    values = {
        "actor_id": "7",
        "guild_id": guild_id,
        "kind": ActorKind.human,
        "permissions": ALL,
    }
    values.update(overrides)
    return ApprovalActor(**values)


def _service(tmp_path, repo: Repository, **overrides) -> ContentPlanningService:
    settings = fake_settings(tmp_path, **overrides)
    return ContentPlanningService(repo, settings, clock=lambda: NOW)


async def test_event_plan_creates_a_bounded_template_without_scheduling(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)

    plan = await service.create_event_plan(
        _actor(),
        title="  Monthly community workshop  ",
        brief="A one hour workshop about debugging Discord bots.",
        scheduled_for=FUTURE,
        target_channel_id="2002",
    )

    assert isinstance(plan, EventPlanView)
    assert plan.title == "Monthly community workshop"
    assert plan.state == "draft"
    assert plan.can_archive()
    assert plan.scheduled_for and plan.scheduled_for.startswith("2026-11-05")
    assert plan.target_channel_id == "2002"
    assert plan.agenda and plan.checklist and plan.roles
    assert any("moderator chooses" in item for item in plan.agenda)

    loaded = await service.get_event_plan(_actor(), plan.plan_id)
    assert loaded == plan
    assert [item.plan_id for item in await service.list_event_plans(_actor())] == [plan.plan_id]

    with pytest.raises(EventPlanNotFoundError):
        await service.get_event_plan(_actor(999), plan.plan_id)

    archived = await service.archive_event_plan(_actor(), plan.plan_id)
    assert archived.state == "archived"
    assert archived.can_archive() is False
    actions = {
        str(item["action"]) for item in await repo.recent_audit(guild_id="500", limit=10)
    }
    assert {"planning.event_plan_created", "planning.event_plan_archived"} <= actions


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "ab", "brief": "A sufficiently long brief."},
        {"title": "Valid title", "brief": "tiny"},
        {"title": "Valid title", "brief": "A sufficiently long brief.", "scheduled_for": PAST},
        {"title": "Valid title", "brief": "A sufficiently long brief.", "scheduled_for": "tomorrow"},
    ],
)
async def test_event_plan_rejects_invalid_input(tmp_path, tmp_db, kwargs):
    service = _service(tmp_path, Repository(tmp_db))
    with pytest.raises(ContentPlanError):
        await service.create_event_plan(_actor(), **kwargs)


async def test_calendar_entries_link_to_drafts_and_plans_and_cancel_softly(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)
    plan = await service.create_event_plan(
        _actor(),
        title="Monthly community workshop",
        brief="A one hour workshop about debugging Discord bots.",
    )
    draft_id = await repo.insert_content_draft_with_version(
        guild_id="500",
        target_channel_id="2003",
        content_type="announcement",
        topic="Workshop announcement",
        audience="Community",
        tone="Friendly",
        purpose="Inform members",
        created_by="7",
        created_at=NOW.isoformat(),
        content="Join the workshop.",
        content_hash=content_hash("Join the workshop."),
        validation_json="[]",
    )

    entry = await service.create_calendar_entry(
        _actor(),
        title="Announce the workshop",
        kind=CalendarKind.announcement,
        starts_at=FUTURE,
        ends_at="2026-11-05T19:00:00+00:00",
        notes="  Mention the recording policy.  ",
        draft_id=draft_id,
        event_plan_id=plan.plan_id,
    )

    assert entry.kind is CalendarKind.announcement
    assert entry.state is CalendarState.planned
    assert entry.can_cancel()
    assert entry.target_channel_id == "2003"  # inherited from the linked draft
    assert entry.draft_id == draft_id
    assert entry.event_plan_id == plan.plan_id
    assert entry.notes == "Mention the recording policy."

    assert [item.entry_id for item in await service.list_calendar(_actor())] == [entry.entry_id]

    cancelled = await service.cancel_calendar_entry(_actor(), entry.entry_id)
    assert cancelled.state is CalendarState.cancelled
    assert cancelled.can_cancel() is False
    assert await service.list_calendar(_actor()) == []
    assert [item.entry_id for item in await service.list_calendar(_actor(), include_cancelled=True)] == [
        entry.entry_id
    ]

    with pytest.raises(ContentPlanError):
        await service.cancel_calendar_entry(_actor(), entry.entry_id)


async def test_calendar_rejects_past_or_unowned_links(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo)

    with pytest.raises(ContentPlanError):
        await service.create_calendar_entry(
            _actor(), title="Too late", kind=CalendarKind.post, starts_at=PAST
        )
    with pytest.raises(ContentPlanError):
        await service.create_calendar_entry(
            _actor(),
            title="Bad window",
            kind=CalendarKind.post,
            starts_at=FUTURE,
            ends_at="2026-11-05T17:00:00+00:00",
        )
    with pytest.raises(EventPlanNotFoundError):
        await service.create_calendar_entry(
            _actor(),
            title="Foreign plan",
            kind=CalendarKind.event,
            starts_at=FUTURE,
            event_plan_id=999,
        )


async def test_reply_drafts_are_neutralized_bounded_and_never_sent(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = _service(tmp_path, repo, contextual_reply_max_chars=120)
    provider = FakeProvider(responses=["@everyone " + "Please restart the gateway and retry. " * 10])

    reply = await service.generate_reply_draft(
        _actor(),
        provider,
        source_excerpt="My bot keeps disconnecting from the gateway.",
        purpose="Help without promising a fix.",
        tone="Concise",
        channel_id="2002",
        source_message_id="55",
    )

    assert reply.state == "draft"
    assert reply.can_dismiss()
    assert "@everyone" not in reply.content
    assert "@\u200beveryone" in reply.content
    assert len(reply.content) <= 120
    assert provider.calls[0]["images"] == []
    assert "My bot keeps disconnecting" in provider.calls[0]["user"]

    with pytest.raises(ContextualReplyNotFoundError):
        await service.get_reply_draft(_actor(999), reply.reply_id)

    dismissed = await service.dismiss_reply_draft(_actor(), reply.reply_id)
    assert dismissed.state == "dismissed"
    assert dismissed.can_dismiss() is False
    assert await service.list_reply_drafts(_actor()) == []
    with pytest.raises(ContextualReplyNotFoundError):
        await service.dismiss_reply_draft(_actor(), reply.reply_id)

    audits = await repo.recent_audit(guild_id="500", limit=10)
    assert "planning.reply_draft_created" in {str(item["action"]) for item in audits}


async def test_reply_draft_rejects_short_source_and_non_human_actors(tmp_path, tmp_db):
    service = _service(tmp_path, Repository(tmp_db))
    provider = FakeProvider(responses=["ok"])

    with pytest.raises(ContentPlanError):
        await service.generate_reply_draft(
            _actor(), provider, source_excerpt="hi", purpose="", tone=""
        )
    with pytest.raises(ApprovalDeniedError):
        await service.generate_reply_draft(
            _actor(kind=ActorKind.agent, permissions=frozenset({ApprovalPermission.propose})),
            provider,
            source_excerpt="A real question from the community.",
            purpose="",
            tone="",
        )
    with pytest.raises(ApprovalDeniedError):
        await service.create_event_plan(
            _actor(permissions=frozenset({ApprovalPermission.decide})),
            title="Unauthorized plan",
            brief="This actor cannot propose planning changes.",
        )
