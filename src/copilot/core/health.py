"""Truthful subsystem health used by /status.

Every field reflects a real check at call time; nothing is hardcoded success.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.database import Database
from ..llm.base import LLMProvider, ModelCapabilities
from .queue import InferenceQueue


@dataclass(frozen=True)
class OllamaSection:
    reachable: bool
    version: str | None
    model: str
    model_exists: bool
    supports_vision: bool
    context_length: int | None
    detail: str | None = None


@dataclass(frozen=True)
class DatabaseSection:
    ok: bool
    path: str
    user_version: int
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class QueueSection:
    pending: int
    capacity: int
    active: bool
    processed_total: int
    cancelled_total: int
    failed_total: int
    cooldown_remaining_s: float = 0.0


@dataclass(frozen=True)
class HealthReport:
    ollama: OllamaSection
    database: DatabaseSection
    queue: QueueSection
    bot_latency_s: float = float("nan")
    guild_count: int = 0
    version: str = ""


async def gather_health(
    *,
    provider: LLMProvider,
    db: Database,
    queue: InferenceQueue,
    cooldown_for: int | None = None,
    bot_latency_s: float = float("nan"),
    guild_count: int = 0,
    version: str = "",
) -> HealthReport:
    health = await provider.health()
    if health.reachable:
        capabilities: ModelCapabilities = await provider.capabilities()
    else:
        capabilities = ModelCapabilities(
            exists=False, name="", error=health.error or "server unreachable"
        )

    db_ok = False
    counts: dict[str, int] = {}
    user_version = 0
    db_error: str | None = None
    try:
        db_ok = await db.ping()
        if db_ok:
            counts = await db.counts()
            user_version = await db.user_version()
        else:
            db_error = "ping failed"
    except Exception as exc:  # noqa: BLE001 - health must never raise
        db_ok = False
        db_error = f"{type(exc).__name__}"

    snapshot = queue.snapshot(cooldown_for=cooldown_for)

    return HealthReport(
        ollama=OllamaSection(
            reachable=health.reachable,
            version=health.version,
            model=capabilities.name or "",
            model_exists=capabilities.exists,
            supports_vision=capabilities.supports_vision,
            context_length=capabilities.context_length,
            detail=capabilities.error or health.error,
        ),
        database=DatabaseSection(
            ok=db_ok, path=str(db.path), user_version=user_version, counts=counts, error=db_error
        ),
        queue=QueueSection(
            pending=snapshot.pending,
            capacity=snapshot.capacity,
            active=snapshot.active_job_id is not None,
            processed_total=snapshot.processed_total,
            cancelled_total=snapshot.cancelled_total,
            failed_total=snapshot.failed_total,
            cooldown_remaining_s=snapshot.cooldown_remaining_s,
        ),
        bot_latency_s=bot_latency_s,
        guild_count=guild_count,
        version=version,
    )
