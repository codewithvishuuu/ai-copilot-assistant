"""Community insights with a hard minimum-data gate.

Below the configured minimum of distinct summarized threads, the engine says so
explicitly instead of showing statistically meaningless noise. No user
profiling: aggregates only (status distribution, recurring error signatures,
threads with open questions).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..db.repository import Repository

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsightsResult:
    window_days: int
    minimum_threads: int
    total_threads: int
    sufficient: bool
    status_counts: dict[str, int] = field(default_factory=dict)
    top_errors: list[tuple[str, int]] = field(default_factory=list)
    threads_with_open_questions: int = 0


async def compute_insights(
    repo: Repository, *, window_days: int, minimum_threads: int
) -> InsightsResult:
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    rows = await repo.summaries_since(since)

    thread_ids = {int(row["thread_id"]) for row in rows}
    total_threads = len(thread_ids)
    if total_threads < minimum_threads:
        return InsightsResult(
            window_days=window_days,
            minimum_threads=minimum_threads,
            total_threads=total_threads,
            sufficient=False,
        )

    status_counts = Counter(str(row["status"]) for row in rows)
    errors: Counter[str] = Counter()
    open_questions = 0

    for row in rows:
        try:
            signatures = json.loads(row["error_signatures"] or "[]")
        except json.JSONDecodeError:
            signatures = []
        for signature in signatures:
            if isinstance(signature, str) and signature.strip():
                errors[signature.strip()[:120]] += 1
        try:
            analysis = json.loads(row["analysis_json"] or "{}")
            if isinstance(analysis, dict) and analysis.get("unanswered_questions"):
                open_questions += 1
        except json.JSONDecodeError:
            log.debug("Summary row %s has unreadable analysis_json", row["id"])

    return InsightsResult(
        window_days=window_days,
        minimum_threads=minimum_threads,
        total_threads=total_threads,
        sufficient=True,
        status_counts=dict(status_counts),
        top_errors=errors.most_common(5),
        threads_with_open_questions=open_questions,
    )
