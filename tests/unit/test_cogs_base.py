"""Regression tests for `CopilotCog.reply` — the live /status crash (CC-8480).

Live failure recorded in data/logs/copilot.log:
    TypeError: Cannot mix embed and embeds keyword arguments.  (discord/http.py:168)
`reply()` forwarded both `embed=None` and `embeds=[...]` to discord.py, whose MISSING
sentinel treats an explicit None as "provided". The fakes mirror that rule, so the
pre-fix code fails these tests exactly like it failed on Discord.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
import pytest
from fakes import FakeInteraction

from copilot.cogs.base import CopilotCog


def make_cog() -> CopilotCog:
    return CopilotCog(bot=SimpleNamespace())


async def test_reply_multi_embeds_after_defer_cc8480_regression():
    """/status shape: defer first, then answer with an embeds list — must not raise."""
    interaction = FakeInteraction()
    await interaction.response.defer(ephemeral=True)
    e1, e2 = discord.Embed(title="page 1"), discord.Embed(title="page 2")

    await make_cog().reply(interaction, embeds=[e1, e2])

    assert interaction.response.sent == []
    recorded = interaction.followup.sent[-1]
    assert recorded["embeds"] == [e1, e2]
    assert recorded["embed"] is None


async def test_reply_multi_embeds_without_defer():
    interaction = FakeInteraction()
    e1 = discord.Embed(title="page 1")

    await make_cog().reply(interaction, embeds=[e1])

    recorded = interaction.response.sent[-1]
    assert recorded["embeds"] == [e1]
    assert recorded["embed"] is None


async def test_reply_single_embed():
    interaction = FakeInteraction()
    e1 = discord.Embed(title="only")

    await make_cog().reply(interaction, embed=e1)

    recorded = interaction.response.sent[-1]
    assert recorded["embed"] is e1
    assert recorded["embeds"] is None


async def test_reply_content_only():
    interaction = FakeInteraction()

    await make_cog().reply(interaction, content="hello", ephemeral=False)

    recorded = interaction.response.sent[-1]
    assert recorded["content"] == "hello"
    assert recorded["embed"] is None
    assert recorded["embeds"] is None


async def test_reply_still_rejects_both_forms_at_once():
    interaction = FakeInteraction()
    e1 = discord.Embed(title="only")

    with pytest.raises(ValueError, match="not both"):
        await make_cog().reply(interaction, embed=e1, embeds=[e1])


async def test_fakes_mirror_discord_py_mixing_rule():
    """The fake must raise like discord.py when both kwargs are passed (even None)."""
    interaction = FakeInteraction()
    e1 = discord.Embed(title="only")

    with pytest.raises(TypeError, match="Cannot mix embed and embeds"):
        await interaction.followup.send(content=None, embed=None, embeds=[e1])


async def test_reply_http_exception_is_swallowed_and_logged_with_detail(caplog):
    """Regression: the old warning swallowed the error, making live reply failures
    like the /cancel "did not respond" incident impossible to diagnose."""
    interaction = FakeInteraction()

    async def broken_send(*args, **kwargs):
        raise discord.HTTPException(
            SimpleNamespace(status=502, reason="Bad Gateway"), "Bad Gateway"
        )

    interaction.response.send_message = broken_send

    with caplog.at_level(logging.WARNING, logger="copilot.cogs.base"):
        await make_cog().reply(interaction, content="hello")  # must not raise

    assert "Could not deliver interaction reply" in caplog.text
    assert "502" in caplog.text  # the discarded status is now recorded
