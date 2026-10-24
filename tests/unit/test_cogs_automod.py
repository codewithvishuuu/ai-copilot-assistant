"""AI moderation scope and command defaults (offline, no Discord)."""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeChatMessage, FakeInteraction, bot_user, fake_settings, make_user

from copilot.cogs.automod import AutomodCog
from copilot.core.moderation_ai import ModerationPolicy


class FakeModerationService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            report=None,
            warning_created=False,
            notify_user=False,
            created=False,
            incident_id=None,
            action_taken="none",
        )


class FakePolicyService:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def update_policy(self, **kwargs) -> ModerationPolicy:
        self.kwargs = kwargs
        return ModerationPolicy(
            guild_id="500",
            enabled=bool(kwargs["enabled"]),
            report_channel_id=None,
            delete_enabled=bool(kwargs["delete_enabled"]),
            severity_threshold=kwargs["severity_threshold"],
            confidence_threshold=kwargs["confidence_threshold"],
            categories=tuple(kwargs["categories"]),
            max_incidents_per_day=kwargs["max_incidents_per_day"],
            notify_user=kwargs["notify_user"],
        )


def make_cog(tmp_path, service, **overrides) -> AutomodCog:
    settings = fake_settings(tmp_path, moderation_enabled=True, **overrides)
    return AutomodCog(
        bot=SimpleNamespace(
            settings=settings,
            moderation_service=service,
            user=bot_user(),
        )
    )


def _message(*, message_id: int, content: str, mentions=()) -> FakeChatMessage:
    return FakeChatMessage(
        id=message_id,
        content=content,
        author_id=7,
        guild_id=500,
        channel_id=2002,
        mentions=list(mentions),
    )


async def test_ordinary_messages_are_out_of_scope(tmp_path):
    service = FakeModerationService()
    cog = make_cog(tmp_path, service)

    await cog._handle(_message(message_id=1, content="ordinary channel message"))
    await cog._handle(
        _message(
            message_id=2,
            content="message mentioning someone else",
            mentions=[SimpleNamespace(id=42)],
        )
    )
    await cog._handle(_message(message_id=3, content="", mentions=[bot_user()]))

    assert service.calls == []


async def test_messages_mentioning_the_bot_are_assessed(tmp_path):
    service = FakeModerationService()
    cog = make_cog(tmp_path, service)

    await cog._handle(
        _message(message_id=4, content="please review this", mentions=[bot_user()])
    )

    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["content"] == "please review this"
    assert call["guild_id"] == 500
    assert call["channel_id"] == 2002


async def test_policy_command_uses_the_configured_confidence_default(tmp_path):
    service = FakePolicyService()
    cog = make_cog(tmp_path, service, moderation_default_confidence=0.42)
    interaction = FakeInteraction(user=make_user(7, manage_threads=True))

    await AutomodCog.policy_command.callback(cog, interaction, True)

    assert service.kwargs is not None
    assert service.kwargs["confidence_threshold"] == 0.42
    assert "confidence ≥ **0.42**" in interaction.followup.sent[-1]["content"]


async def test_policy_command_explicit_confidence_wins(tmp_path):
    service = FakePolicyService()
    cog = make_cog(tmp_path, service, moderation_default_confidence=0.42)
    interaction = FakeInteraction(user=make_user(7, manage_threads=True))

    await AutomodCog.policy_command.callback(cog, interaction, True, confidence=0.9)

    assert service.kwargs is not None
    assert service.kwargs["confidence_threshold"] == 0.9
