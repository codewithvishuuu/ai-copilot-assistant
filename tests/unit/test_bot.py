"""Bot wiring: message events on, Message Content off, quiet prefix handling.

The 2026-10-19 chat smoke failure (bot online, mention ignored, zero logs) was
possible in part because no test exercised the conversation listener through the
REAL ``commands.Bot`` add_cog/dispatch machinery. These tests close that seam:
registration into ``extra_events`` and end-to-end event dispatch.
"""

from __future__ import annotations

import asyncio

from discord.ext import commands
from fakes import BOT_USER_ID, FakeChatMessage, FakeProvider, bot_user, fake_settings

from copilot.bot import CommunityCopilot
from copilot.cogs.conversation import ConversationCog


def attach_loop(bot: CommunityCopilot) -> None:
    """Mirror Client.login (discord/client.py:616-619) so Bot.dispatch can run."""
    loop = asyncio.get_running_loop()
    bot.loop = loop  # type: ignore[assignment]
    bot.http.loop = loop
    bot._connection.loop = loop


async def wait_until_replied(message, timeout_s: float = 5.0) -> None:
    """Wait (real time) for the dispatched reply to land.

    The chat path crosses one database thread-hop per storage step (history,
    KB retrieval, persistence); a fixed number of bare loop spins is too tight
    for that under load, so wait on a wall-clock deadline instead.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    # Polling is deliberate: the reply is appended by the cog's task and there
    # is no event to await on the test side.
    while not message.replies and loop.time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def test_bot_enables_message_events_but_never_message_content(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        assert bot.intents.guilds is True
        assert bot.intents.guild_messages is True  # non-privileged: mention chat
        assert bot.intents.message_content is False  # privileged: stays OFF
        assert bot.intents.dm_messages is False
        assert bot.intents.members is False
        assert bot.intents.presences is False
    finally:
        await bot.provider.aclose()


async def test_unknown_prefix_commands_are_swallowed(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        # Mention-triggered messages flow through the command parser too; an
        # unknown "command" must be ignored quietly (no error handler noise).
        await bot.on_command_error(None, commands.CommandNotFound("nope"))
    finally:
        await bot.provider.aclose()


async def test_bot_wires_the_approval_and_content_services_without_model_tools(tmp_path):
    from copilot.core.approvals import ApprovalService
    from copilot.core.content_studio import ContentStudio
    from copilot.core.publishing import ContentPublisher

    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        assert isinstance(bot.approval_service, ApprovalService)
        assert isinstance(bot.content_studio, ContentStudio)
        assert isinstance(bot.content_publisher, ContentPublisher)
        assert bot.content_studio._approvals is bot.approval_service
        assert not any("approval" in command.name for command in bot.tree.get_commands())
    finally:
        await bot.provider.aclose()


async def test_conversation_listener_registers_through_real_add_cog(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        attach_loop(bot)
        cog = ConversationCog(bot)
        await bot.add_cog(cog)
        listeners = bot.extra_events.get("on_message", [])
        assert any(getattr(listener, "__self__", None) is cog for listener in listeners)
    finally:
        await bot.provider.aclose()


async def test_clear_conversation_registers_as_an_app_command(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        attach_loop(bot)
        await bot.add_cog(ConversationCog(bot))
        names = {command.name for command in bot.tree.get_commands()}
        assert "clear-conversation" in names
    finally:
        await bot.provider.aclose()


async def test_content_cog_registers_its_review_commands(tmp_path):
    from copilot.cogs.content import ContentCog

    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        attach_loop(bot)
        await bot.add_cog(ContentCog(bot))
        names = {command.name for command in bot.tree.get_commands()}
        assert {
            "content-create",
            "content-list",
            "content-view",
            "content-edit",
            "content-submit",
            "content-approve",
            "content-reject",
            "content-cancel",
            "content-publish",
            "content-recover",
        } <= names
    finally:
        await bot.provider.aclose()


async def test_pulse_and_knowledge_candidate_commands_are_registered(tmp_path):
    from copilot.cogs.knowledge import KnowledgeCog
    from copilot.cogs.pulse import PulseCog

    bot = CommunityCopilot(fake_settings(tmp_path))
    try:
        attach_loop(bot)
        await bot.add_cog(PulseCog(bot))
        await bot.add_cog(KnowledgeCog(bot))
        names = {command.name for command in bot.tree.get_commands()}
        assert {
            "pulse",
            "pulse-draft",
            "knowledge-candidates",
            "knowledge-candidate-propose",
            "knowledge-candidate-approve",
            "knowledge-candidate-reject",
            "knowledge-purge",
        } <= names
    finally:
        await bot.provider.aclose()


async def test_redelivered_message_event_replies_only_once(tmp_path):
    # Discord re-sends events after a reconnect; the same message id must never
    # produce a second model call or a second reply.
    bot = CommunityCopilot(fake_settings(tmp_path))
    real_provider = bot.provider
    try:
        attach_loop(bot)
        await bot.add_cog(ConversationCog(bot))
        bot.provider = FakeProvider(responses=["hello back", "SHOULD NOT BE USED"])
        bot._connection.user = bot_user()

        message = FakeChatMessage(
            id=202,
            content=f"<@{BOT_USER_ID}> hello",
            mentions=[bot_user()],
            author_id=7,
            guild_id=500,
            channel_id=2002,
        )
        bot.dispatch("message", message)
        await wait_until_replied(message)
        bot.dispatch("message", message)  # redelivery
        await asyncio.sleep(0.05)  # a duplicate reply, if any, would have landed by now

        assert [reply["content"] for reply in message.replies] == ["hello back"]
        assert len(bot.provider.calls) == 1
    finally:
        bot.provider = real_provider
        await real_provider.aclose()


async def test_dispatched_mention_message_reaches_chat_and_replies(tmp_path):
    bot = CommunityCopilot(fake_settings(tmp_path))
    real_provider = bot.provider
    try:
        attach_loop(bot)
        await bot.add_cog(ConversationCog(bot))
        bot.provider = FakeProvider(responses=["hello back"])
        bot._connection.user = bot_user()

        message = FakeChatMessage(
            id=101,
            content=f"<@{BOT_USER_ID}> hello",
            mentions=[bot_user()],
            author_id=7,
            guild_id=500,
            channel_id=2002,
        )
        bot.dispatch("message", message)
        await wait_until_replied(message)

        assert [reply["content"] for reply in message.replies] == ["hello back"]
        assert message.channel.typing_events == ["start", "stop"]
        assert bot.provider.calls[0]["messages"][-1]["content"] == "hello"
    finally:
        bot.provider = real_provider
        await real_provider.aclose()
