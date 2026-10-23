"""Personality module: central, language-aware, injection-resistant, honest."""

from __future__ import annotations

from copilot.llm.persona import EMPTY_REPLY, HELP_REPLY, PERSONA_VERSION, REPLY_HINT, SYSTEM_PERSONA


def test_persona_version_is_pinned():
    assert PERSONA_VERSION == "chat-v2"


def test_system_persona_declares_untrusted_data_rules():
    lowered = SYSTEM_PERSONA.lower()
    assert "untrusted" in lowered
    assert "never follow instructions" in lowered
    assert "system prompt" in lowered  # never reveal it


def test_system_persona_is_language_aware():
    lowered = SYSTEM_PERSONA.lower()
    for token in ("hindi", "hinglish", "english"):
        assert token in lowered


def test_system_persona_is_honest_about_being_a_bot():
    assert "never pretend to be a human" in SYSTEM_PERSONA.lower()


def test_system_persona_denies_privileged_actions():
    lowered = SYSTEM_PERSONA.lower()
    assert "approve or reject solutions" in lowered
    assert "moderator" in lowered


def test_system_persona_declares_kb_grounding_rules():
    lowered = SYSTEM_PERSONA.lower()
    assert "<kb>" in lowered
    assert "reference material" in lowered
    assert "possibly related" in lowered
    assert "never invent" in lowered
    assert "citations" in lowered
    assert "transparent about uncertainty" in lowered


def test_system_persona_extends_untrusted_rules_to_the_kb_block():
    lowered = SYSTEM_PERSONA.lower()
    assert "<kb> context" in lowered
    assert "the safety rules above apply to it exactly" in lowered


def test_static_replies_are_nonempty_and_on_topic():
    assert "mention" in HELP_REPLY.lower()
    assert "@mention" in REPLY_HINT
    assert EMPTY_REPLY.strip()
