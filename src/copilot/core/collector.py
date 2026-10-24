"""Thread collection with hard caps and skip accounting.

The collector is duck-typed against ``discord.Thread`` (so tests can use tiny
fakes). It enforces:
- a hard message cap (``MAX_MESSAGES`` bounded by ``HARD_MAX_MESSAGES``),
- a bounded scan window (system messages are skipped but cost scan budget),
- a total character budget (oldest non-opening messages trimmed first),
- structured skip notes that are surfaced honestly in the final embed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..errors import EmptyThreadError, ThreadInaccessibleError
from ..util.text import truncate

log = logging.getLogger(__name__)

_ALLOWED_MESSAGE_TYPES = {0, 19, 21}  # default, reply, thread_starter_message
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".heic")
# A generic MIME type with an unknown suffix may still be an image (e.g.
# upload.bin); the download path verifies magic bytes before use. Known
# document/text suffixes are not treated as image candidates.
_NON_IMAGE_SUFFIXES = {
    ".pdf", ".docx", ".zip", ".txt", ".md", ".markdown", ".rst", ".csv",
    ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c",
    ".h", ".cpp", ".cs", ".rb", ".php", ".swift", ".sh", ".bash", ".zsh",
    ".ps1", ".bat", ".log", ".out", ".err", ".trace", ".stacktrace",
}


@dataclass
class CollectedAttachment:
    filename: str
    content_type: str | None
    size: int
    url: str
    message_id: str
    raw: Any = field(default=None, repr=False, compare=False)

    def looks_like_image(self) -> bool:
        mime = (self.content_type or "").lower()
        if mime.startswith("image/"):
            return True
        name = self.filename.lower()
        if name.endswith(_IMAGE_EXTS):
            return True
        # Generic MIME types are common for valid uploads. Let the magic-byte
        # validation in the image pipeline decide; only skip known non-images.
        return mime in ("", "application/octet-stream") and not any(
            name.endswith(suffix) for suffix in _NON_IMAGE_SUFFIXES
        )


@dataclass
class CollectedMessage:
    id: str
    author: str
    created_at: str
    content: str
    is_opening: bool = False
    attachments: list[CollectedAttachment] = field(default_factory=list)


@dataclass
class CollectedThread:
    thread_id: str
    guild_id: str
    parent_channel_id: str | None
    thread_name: str
    jump_url: str
    messages: list[CollectedMessage]
    skipped_notes: list[str] = field(default_factory=list)
    truncated: bool = False
    collected_at: str = ""

    def message_ids(self) -> set[str]:
        return {message.id for message in self.messages}

    def image_attachments(self) -> list[CollectedAttachment]:
        return [att for message in self.messages for att in message.attachments if att.looks_like_image()]


# ------------------------------------------------------------------ pure helpers


def _message_type_value(message: Any) -> int:
    message_type = getattr(message, "type", 0)
    try:
        return int(getattr(message_type, "value", message_type) or 0)
    except (TypeError, ValueError):
        return 0


def is_skippable(message: Any) -> str | None:
    """Return a skip reason (``"system"``/``"empty"``) or None when usable."""
    if _message_type_value(message) not in _ALLOWED_MESSAGE_TYPES:
        return "system"
    content = (getattr(message, "content", "") or "").strip()
    attachments = getattr(message, "attachments", None) or []
    if not content and not attachments:
        return "empty"
    return None


def to_collected(message: Any, *, is_opening: bool = False) -> CollectedMessage:
    author_obj = getattr(message, "author", None)
    author = (
        getattr(author_obj, "display_name", None)
        or getattr(author_obj, "name", None)
        or "unknown"
    )
    created = getattr(message, "created_at", None)
    created_at = created.isoformat() if isinstance(created, datetime) else str(created or "")

    attachments: list[CollectedAttachment] = []
    for raw_att in getattr(message, "attachments", None) or []:
        try:
            size = int(getattr(raw_att, "size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        attachments.append(
            CollectedAttachment(
                filename=str(getattr(raw_att, "filename", "") or ""),
                content_type=getattr(raw_att, "content_type", None),
                size=size,
                url=str(getattr(raw_att, "url", "") or ""),
                message_id=str(getattr(message, "id", "")),
                raw=raw_att,
            )
        )

    return CollectedMessage(
        id=str(getattr(message, "id", "")),
        author=str(author),
        created_at=created_at,
        content=getattr(message, "content", "") or "",
        is_opening=is_opening,
        attachments=attachments,
    )


def apply_char_budget(
    messages: list[CollectedMessage], max_chars: int
) -> tuple[list[CollectedMessage], int, bool]:
    """Trim oldest non-opening messages until under budget.

    Returns ``(kept, dropped_count, opening_truncated)``. Messages are expected
    in chronological order with the opening post first.
    """

    def size(message: CollectedMessage) -> int:
        return len(message.content or "")

    total = sum(size(m) for m in messages)
    if total <= max_chars:
        return messages, 0, False

    kept = list(messages)
    dropped = 0
    index = 0
    while total > max_chars and len(kept) > 1:
        if index >= len(kept):
            break
        candidate = kept[index]
        if candidate.is_opening:
            index += 1
            continue
        total -= size(candidate)
        kept.pop(index)
        dropped += 1

    opening_truncated = False
    if kept and total > max_chars:
        first = kept[0]
        if size(first) > max_chars:
            first.content = truncate(first.content, max(0, max_chars), suffix=" …[truncated]")
            opening_truncated = True
    return kept, dropped, opening_truncated


# ------------------------------------------------------------------- collection


async def collect_thread(thread: Any, *, settings: Any, max_messages: int | None = None) -> CollectedThread:
    """Collect the opening post + most recent usable messages from a thread."""
    limit = max_messages if max_messages is not None else settings.max_messages
    limit = max(1, min(int(limit), int(settings.hard_max_messages)))
    scan_limit = min(limit * 3 + 50, 2000)

    thread_id = str(getattr(thread, "id", "") or "")
    guild = getattr(thread, "guild", None)
    guild_id = str(getattr(guild, "id", "")) if guild is not None else ""
    parent_id = getattr(thread, "parent_id", None)
    parent_channel_id = str(parent_id) if parent_id is not None else None
    thread_name = str(getattr(thread, "name", "") or "")
    jump_url = str(getattr(thread, "jump_url", "") or "")
    total_known_raw = getattr(thread, "message_count", None)
    total_known = int(total_known_raw) if isinstance(total_known_raw, int) else None

    async def _fetch() -> tuple[Any, list[Any]]:
        opening = None
        starter = getattr(thread, "starter_message", None)
        if starter is not None:
            opening = starter
        else:
            try:
                opening = await thread.fetch_message(thread.id)
            except Exception:  # noqa: BLE001 - NotFound/Forbidden/HTTPException
                log.info("Opening post unavailable for thread %s", thread_id)
                opening = None
        raw_messages: list[Any] = []
        async for message in thread.history(limit=scan_limit, oldest_first=False):
            raw_messages.append(message)
        return opening, raw_messages

    try:
        opening, raw_messages = await asyncio.wait_for(_fetch(), timeout=settings.fetch_timeout_s)
    except TimeoutError as exc:
        raise ThreadInaccessibleError(
            "Reading that thread took too long. Try again or pass a smaller count.",
            detail="history fetch timeout",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - map Discord errors to a friendly message
        raise ThreadInaccessibleError(detail=f"{type(exc).__name__}: {exc}") from exc

    notes: list[str] = []
    skipped_system = 0
    skipped_empty = 0

    opening_collected: CollectedMessage | None = None
    if opening is not None and is_skippable(opening) is None:
        opening_collected = to_collected(opening, is_opening=True)
    else:
        # Either the opening could not be fetched at all, or it exists but is
        # unusable (system/empty). Either way the summary must say so instead
        # of pretending the thread starts with the replies.
        notes.append("the opening post could not be read; the summary uses replies only")

    seen_ids: set[str] = set()
    if opening_collected is not None:
        seen_ids.add(opening_collected.id)

    recent: list[CollectedMessage] = []
    for raw in raw_messages:
        reason = is_skippable(raw)
        if reason == "system":
            skipped_system += 1
            continue
        if reason == "empty":
            skipped_empty += 1
            continue
        collected = to_collected(raw)
        if collected.id in seen_ids:
            continue
        seen_ids.add(collected.id)
        recent.append(collected)
        if len(recent) >= limit:
            break

    recent.reverse()  # chronological order
    messages: list[CollectedMessage] = []
    if opening_collected is not None:
        messages.append(opening_collected)
    messages.extend(recent)

    if not messages:
        raise EmptyThreadError()

    messages, dropped, opening_truncated = apply_char_budget(messages, settings.max_chars)
    truncated = False

    if skipped_system:
        notes.append(f"{skipped_system} system/bot event message(s) skipped")
    if skipped_empty:
        notes.append(f"{skipped_empty} empty message(s) skipped")
    if dropped:
        truncated = True
        notes.append(f"{dropped} older message(s) trimmed to fit the character budget")
    if opening_truncated:
        truncated = True
        notes.append("the opening post was truncated to fit the character budget")
    if total_known is not None and total_known > len(messages):
        notes.append(
            f"thread has about {total_known} messages; the {len(messages)} most recent "
            "usable message(s) were analyzed"
        )

    collected_thread = CollectedThread(
        thread_id=thread_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        thread_name=thread_name,
        jump_url=jump_url,
        messages=messages,
        skipped_notes=notes,
        truncated=truncated,
        collected_at=datetime.now(UTC).isoformat(),
    )
    log.info(
        "Collected thread %s: %d message(s), %d note(s)",
        thread_id,
        len(messages),
        len(notes),
    )
    return collected_thread
