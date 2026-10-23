"""Guild voice preferences, moderator feedback, and near-duplicate detection.

Voice and feedback are guild-scoped application data, never model authority.
Both are injected into the content prompt as untrusted constraints; the system
prompt still wins. Near-duplicate detection compares token shingles against
recent approved/submitted drafts and posts in the same guild. It is advisory
(a visible warning), while exact SHA-256 equality remains a separate check.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..db.repository import Repository
from ..util.text import sanitize_for_prompt, truncate

ALLOWED_EMOJI_POLICIES = ("none", "sparing", "allowed")
MAX_STYLE_NOTES = 1_500
MAX_BANNED_PHRASES = 40
MAX_FEEDBACK_CHARS = 1_000
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+#.-]{1,}", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceProfile:
    tone: str = ""
    audience: str = ""
    emoji_policy: str = "sparing"
    formatting: str = ""
    style_notes: str = ""
    banned_phrases: tuple[str, ...] = ()
    updated_by: str = ""

    @classmethod
    def from_row(cls, row: Any) -> VoiceProfile:
        if row is None:
            return cls()
        try:
            banned = tuple(
                sanitize_for_prompt(str(item))[:100]
                for item in json.loads(row["banned_phrases_json"] or "[]")
                if str(item).strip()
            )[:MAX_BANNED_PHRASES]
        except (TypeError, ValueError, json.JSONDecodeError):
            banned = ()
        policy = str(row["emoji_policy"] or "sparing")
        return cls(
            tone=sanitize_for_prompt(str(row["tone"] or ""))[:100],
            audience=sanitize_for_prompt(str(row["audience"] or ""))[:200],
            emoji_policy=policy if policy in ALLOWED_EMOJI_POLICIES else "sparing",
            formatting=sanitize_for_prompt(str(row["formatting"] or ""))[:200],
            style_notes=sanitize_for_prompt(str(row["style_notes"] or ""))[:MAX_STYLE_NOTES],
            banned_phrases=banned,
            updated_by=str(row["updated_by"] or ""),
        )

    def prompt_lines(self) -> list[str]:
        lines: list[str] = []
        if self.tone:
            lines.append(f"preferred_tone: {self.tone}")
        if self.audience:
            lines.append(f"preferred_audience: {self.audience}")
        lines.append(f"emoji_policy: {self.emoji_policy}")
        if self.formatting:
            lines.append(f"formatting: {self.formatting}")
        if self.style_notes:
            lines.append(f"style_notes: {self.style_notes}")
        if self.banned_phrases:
            lines.append("banned_phrases: " + "; ".join(self.banned_phrases))
        return lines


async def load_voice_profile(repo: Repository, guild_id: int | str) -> VoiceProfile:
    return VoiceProfile.from_row(await repo.get_voice_profile(guild_id=str(guild_id)))


async def save_voice_profile(
    repo: Repository,
    *,
    guild_id: int | str,
    updated_by: str,
    tone: str,
    audience: str,
    emoji_policy: str,
    formatting: str,
    style_notes: str,
    banned_phrases: list[str],
    now: str,
) -> VoiceProfile:
    policy = str(emoji_policy or "sparing").lower()
    if policy not in ALLOWED_EMOJI_POLICIES:
        raise ValueError("emoji policy must be none, sparing or allowed")
    cleaned_phrases = [
        truncate(sanitize_for_prompt(item), 100)
        for item in banned_phrases
        if sanitize_for_prompt(item)
    ][:MAX_BANNED_PHRASES]
    await repo.upsert_voice_profile(
        guild_id=str(guild_id),
        tone=truncate(sanitize_for_prompt(tone), 100),
        audience=truncate(sanitize_for_prompt(audience), 200),
        emoji_policy=policy,
        formatting=truncate(sanitize_for_prompt(formatting), 200),
        style_notes=truncate(sanitize_for_prompt(style_notes), MAX_STYLE_NOTES),
        banned_phrases_json=json.dumps(cleaned_phrases),
        updated_by=str(updated_by),
        now=now,
    )
    return await load_voice_profile(repo, guild_id)


def tokens(text: str) -> list[str]:
    cleaned = _URL_RE.sub(" ", (text or "").lower())
    return _WORD_RE.findall(cleaned)


def shingles(text: str, *, size: int = 3) -> set[tuple[str, ...]]:
    words = tokens(text)
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


def similarity_ratio(left: str, right: str) -> float:
    """Jaccard similarity over word 3-shingles (0..1), deterministic and bounded."""
    left_set = shingles(left)
    right_set = shingles(right)
    if not left_set or not right_set:
        return 0.0
    intersection = len(left_set & right_set)
    union = len(left_set | right_set)
    return intersection / union if union else 0.0


def recent_feedback_prompt(rows: list[Any], *, limit_chars: int = 1_500) -> list[str]:
    out: list[str] = []
    used = 0
    for row in rows:
        try:
            text = sanitize_for_prompt(str(row["feedback"] or ""))
        except (KeyError, IndexError, TypeError):
            continue
        if not text:
            continue
        text = truncate(text, MAX_FEEDBACK_CHARS)
        if used + len(text) > limit_chars:
            text = truncate(text, max(0, limit_chars - used))
        if not text:
            break
        out.append(text)
        used += len(text)
        if used >= limit_chars:
            break
    return out
