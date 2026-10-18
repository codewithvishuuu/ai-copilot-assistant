"""Text sanitizing helpers for untrusted community content.

Every string that originates from Discord (messages, thread names, author names,
attachments metadata) or from the knowledge base passes through these helpers
before it can enter a prompt or an embed.
"""

from __future__ import annotations

import re

_CONTROL_TOKEN_RE = re.compile(r"<\|[^|\n]{0,64}\|>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MENTION_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_FTS_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "was",
    "were", "are", "not", "but", "you", "your", "can", "how", "what", "when",
    "why", "where", "which", "will", "would", "could", "should", "about", "into",
    "over", "under", "after", "before", "then", "than", "them", "they", "their",
    "there", "here", "while", "also", "any", "all", "get", "got", "use", "used",
    "using", "its", "onto", "one", "two", "out", "off", "again", "just", "only",
    "some", "such", "more", "most", "other", "same", "does", "did", "doing",
    "been", "being", "because", "very", "much", "still", "even", "ever", "each",
}


def strip_control_tokens(text: str) -> str:
    """Remove model control tokens (e.g. ``<|think|>``, ``<|channel|>``) from content."""
    return _CONTROL_TOKEN_RE.sub("", text or "")


def normalize_whitespace(text: str) -> str:
    """Normalize newlines and collapse runs of spaces/tabs."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def sanitize_for_prompt(text: str) -> str:
    """Full sanitize pass for untrusted content entering a prompt."""
    return normalize_whitespace(strip_control_tokens(text or ""))


def neutralize_mentions(text: str) -> str:
    """Defuse ``@everyone``/``@here`` for display (belt-and-braces; sends also disable mentions)."""
    return _MENTION_RE.sub(lambda m: "@\u200b" + m.group(1), text or "")


def truncate(text: str, limit: int, suffix: str = " …[truncated]") -> str:
    """Truncate ``text`` to at most ``limit`` characters, including the suffix."""
    if limit <= 0:
        return ""
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def is_probably_empty(text: str | None) -> bool:
    """True when text is None/blank/whitespace-only."""
    return text is None or not text.strip()


def build_fts_query(text: str, *, max_terms: int = 10, min_length: int = 3) -> str | None:
    """Turn free text into a safe FTS5 OR-query, or None when there is nothing usable.

    Tokens are restricted to ``[A-Za-z0-9_]`` and quoted, so no FTS5 syntax can
    leak through from untrusted content.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _FTS_TOKEN_RE.findall(text or ""):
        token = raw.lower()
        if len(token) < min_length or token in _FTS_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    if not tokens:
        return None
    tokens.sort(key=len, reverse=True)  # longer tokens tend to carry the meaning
    return " OR ".join(f'"{token}"' for token in tokens[:max_terms])
