"""Health: every section reflects a real check, and health never raises."""

from __future__ import annotations

from fakes import FakeProvider

from copilot.core.health import gather_health
from copilot.core.queue import InferenceQueue


class _ExplodingCapabilities(FakeProvider):
    async def capabilities(self, *, force_refresh=False):
        raise AssertionError("capabilities() must not be called when the server is unreachable")


def _queue(**overrides):
    return InferenceQueue(
        capacity=overrides.pop("capacity", 3),
        per_user_cooldown_s=overrides.pop("per_user_cooldown_s", 0),
        per_guild_hourly_limit=overrides.pop("per_guild_hourly_limit", 5),
        job_timeout_s=overrides.pop("job_timeout_s", 1.0),
    )


async def test_healthy_report_reflects_all_subsystems(tmp_db):
    provider = FakeProvider(
        reachable=True, version="9.9.9", model_exists=True, vision=True, context_length=131072
    )
    report = await gather_health(
        provider=provider,
        db=tmp_db,
        queue=_queue(),
        bot_latency_s=0.25,
        guild_count=2,
        version="0.1.0",
    )
    assert report.ollama.reachable is True
    assert report.ollama.version == "9.9.9"
    assert report.ollama.model_exists is True
    assert report.ollama.supports_vision is True
    assert report.ollama.context_length == 131072
    assert report.ollama.detail is None

    assert report.database.ok is True
    assert report.database.user_version == 2
    assert report.database.counts["threads"] == 0
    assert report.database.path.endswith("test.db")

    assert report.queue.pending == 0
    assert report.queue.capacity == 3
    assert report.queue.active is False

    assert report.bot_latency_s == 0.25
    assert report.guild_count == 2
    assert report.version == "0.1.0"


async def test_unreachable_ollama_skips_capabilities_and_reports_detail(tmp_db):
    provider = _ExplodingCapabilities(reachable=False)
    report = await gather_health(provider=provider, db=tmp_db, queue=_queue())
    assert report.ollama.reachable is False
    assert report.ollama.model_exists is False
    assert report.ollama.supports_vision is False
    assert report.ollama.detail == "connection refused"
    assert report.database.ok is True  # the rest of the report still works


async def test_missing_model_is_reported_truthfully(tmp_db):
    provider = FakeProvider(reachable=True, model_exists=False)
    report = await gather_health(provider=provider, db=tmp_db, queue=_queue())
    assert report.ollama.reachable is True
    assert report.ollama.model_exists is False
    assert report.ollama.detail == "model not installed"


async def test_database_failure_is_reported_and_never_raises(tmp_path):
    from copilot.db.database import Database

    db = Database(tmp_path / "closed.db")
    await db.connect()
    await db.close()

    report = await gather_health(provider=FakeProvider(), db=db, queue=_queue())
    assert report.database.ok is False
    assert report.database.counts == {}
    assert report.database.error == "ping failed"
