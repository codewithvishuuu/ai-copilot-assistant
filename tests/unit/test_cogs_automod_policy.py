"""`/moderation-policy` must merge omitted options, not reset them."""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeInteraction, bot_user, fake_settings, make_user

from copilot.cogs.automod import AutomodCog
from copilot.core.moderation_ai import ModerationPolicy


class FakePolicyService:
    def __init__(self) -> None:
        self.existing = ModerationPolicy(
            guild_id="500",
            enabled=True,
            report_channel_id="123",
            delete_enabled=True,
            severity_threshold="critical",
            confidence_threshold=0.9,
            categories=("spam",),
            max_incidents_per_day=5,
            notify_user=True,
        )
        self.kwargs: dict | None = None

    async def policy(self, guild_id):
        return self.existing

    async def update_policy(self, **kwargs):
        self.kwargs = kwargs
        return ModerationPolicy(
            guild_id="500",
            enabled=bool(kwargs["enabled"]),
            report_channel_id=kwargs["report_channel_id"],
            delete_enabled=bool(kwargs["delete_enabled"]),
            severity_threshold=kwargs["severity_threshold"],
            confidence_threshold=kwargs["confidence_threshold"],
            categories=tuple(kwargs["categories"]),
            max_incidents_per_day=kwargs["max_incidents_per_day"],
            notify_user=kwargs["notify_user"],
        )


def make_cog(tmp_path, service) -> AutomodCog:
    return AutomodCog(
        bot=SimpleNamespace(
            settings=fake_settings(tmp_path, moderation_enabled=True),
            moderation_service=service,
            user=bot_user(),
        )
    )


async def test_enabled_only_preserves_every_existing_policy_field(tmp_path):
    service = FakePolicyService()
    cog = make_cog(tmp_path, service)
    interaction = FakeInteraction(user=make_user(7, manage_threads=True))

    await AutomodCog.policy_command.callback(cog, interaction, True)

    assert service.kwargs is not None
    assert service.kwargs["report_channel_id"] == "123"
    assert service.kwargs["delete_enabled"] is True
    assert service.kwargs["severity_threshold"] == "critical"
    assert service.kwargs["confidence_threshold"] == 0.9
    assert service.kwargs["categories"] == ["spam"]
    assert service.kwargs["max_incidents_per_day"] == 5
    assert service.kwargs["notify_user"] is True


async def test_clear_report_channel_only_clears_that_field(tmp_path):
    service = FakePolicyService()
    cog = make_cog(tmp_path, service)
    interaction = FakeInteraction(user=make_user(7, manage_threads=True))

    await AutomodCog.policy_command.callback(
        cog, interaction, True, clear_report_channel=True
    )

    assert service.kwargs is not None
    assert service.kwargs["report_channel_id"] is None
    assert service.kwargs["delete_enabled"] is True
    assert service.kwargs["categories"] == ["spam"]
