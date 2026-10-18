"""Text utilities: control-token stripping, mention neutralization, truncation, FTS queries."""

from __future__ import annotations

from copilot.util.text import (
    build_fts_query,
    is_probably_empty,
    neutralize_mentions,
    sanitize_for_prompt,
    strip_control_tokens,
    truncate,
)


def test_strip_control_tokens():
    assert strip_control_tokens("hello <|think|>world") == "hello world"
    assert strip_control_tokens("<|channel|>x") == "x"
    assert strip_control_tokens("keep < not a token") == "keep < not a token"


def test_sanitize_normalizes_whitespace_and_tokens():
    messy = "line1\r\nline2   spaced\r\n\r\n\r\n\r\nline3 <|think|>"
    out = sanitize_for_prompt(messy)
    assert out == "line1\nline2 spaced\n\nline3"
    assert "\r" not in out


def test_neutralize_mentions():
    out = neutralize_mentions("hey @everyone and @Here plus @someone")
    assert "@\u200beveryone" in out
    assert "@\u200bHere" in out
    assert "@someone" in out  # regular user mentions are not touched here


def test_truncate_respects_limit_including_suffix():
    text = "x" * 100
    out = truncate(text, 20, suffix=" …[cut]")
    assert len(out) <= 20
    assert out.endswith(" …[cut]")


def test_truncate_small_limit_edge_case():
    assert truncate("abcdef", 0) == ""
    assert len(truncate("abcdef", 4)) <= 4


def test_is_probably_empty():
    assert is_probably_empty(None)
    assert is_probably_empty("   \n ")
    assert not is_probably_empty("x")


def test_build_fts_query_basic():
    query = build_fts_query("Login crash on refresh token")
    assert query is not None
    assert " OR " in query
    assert query.count('"') == 2 * len(query.split(" OR "))


def test_build_fts_query_drops_stopwords_and_short_tokens():
    query = build_fts_query("the a of it in on table drop")
    assert query == '"table" OR "drop"'


def test_build_fts_query_exact_ordering_prefers_longer_terms():
    query = build_fts_query("Login crash on refresh token")
    assert query == '"refresh" OR "login" OR "crash" OR "token"'


def test_build_fts_query_none_for_junk():
    assert build_fts_query("!!!") is None
    assert build_fts_query("") is None
    assert build_fts_query(None) is None


def test_build_fts_query_neutralizes_syntax():
    query = build_fts_query('error"; DROP TABLE kb_docs; --')
    assert query is not None
    assert '";' not in query
    assert ";" not in query
    assert query.count('"') == 2 * len(query.split(" OR "))
