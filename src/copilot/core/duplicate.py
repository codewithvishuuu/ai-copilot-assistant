"""Possibly-related issue detection over the knowledge base (SQLite FTS5 / BM25).

Lexical only — documented trade-off (see RAG.md). Results are always labeled
"possibly related" with evidence links; nothing is merged automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.repository import Repository
from ..llm.schemas import ThreadAnalysis


@dataclass(frozen=True)
class RelatedDoc:
    kind: str
    title: str
    url: str | None
    snippet: str
    score: float


async def find_related(
    repo: Repository,
    text: str,
    *,
    guild_id: str,
    exclude_discord_thread_id: str | None = None,
    limit: int = 3,
) -> list[RelatedDoc]:
    if not text or not text.strip():
        return []
    rows = await repo.related_docs(
        text,
        guild_id=guild_id,
        exclude_discord_thread_id=exclude_discord_thread_id,
        limit=limit,
    )
    return [
        RelatedDoc(
            kind=str(row["kind"]),
            title=str(row["title"] or ""),
            url=row["url"],
            snippet=str(row["snippet"] or ""),
            score=float(row["score"] or 0.0),
        )
        for row in rows
    ]


def fingerprint_text(analysis: ThreadAnalysis) -> str:
    """Compact issue fingerprint indexed for future duplicate detection."""
    parts = [analysis.overview, analysis.main_problem]
    parts.extend(item.text for item in analysis.errors[:3])
    return " ".join(part for part in parts if part)
