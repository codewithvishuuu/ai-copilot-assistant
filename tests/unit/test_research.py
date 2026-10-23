"""Web research: URL safety, provider parsing, synthesis grounding, provenance."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fakes import FakeProvider, fake_settings

from copilot.core.approvals import (
    ActorKind,
    ApprovalActor,
    ApprovalPermission,
    ApprovalService,
)
from copilot.core.content_studio import ContentStudio, ContentTarget
from copilot.db.repository import Repository
from copilot.errors import (
    ResearchNoResultsError,
    ResearchProviderError,
    ResearchReportNotFoundError,
    ResearchUnavailableError,
)
from copilot.research.provider import (
    BraveSearchProvider,
    TavilySearchProvider,
    build_web_search_provider,
    safe_source_url,
)
from copilot.research.service import (
    ResearchService,
    ResearchStatus,
    create_draft_from_research,
    load_sources,
)

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
ALL = frozenset(
    {
        ApprovalPermission.propose,
        ApprovalPermission.decide,
        ApprovalPermission.cancel,
        ApprovalPermission.execute,
    }
)


class FakeSearchProvider:
    name = "fake"

    def __init__(self, results=(), error: BaseException | None = None) -> None:
        self.results = list(results)
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.results[:limit]


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _settings(tmp_path, **overrides):
    values = {
        "web_research_enabled": True,
        "web_search_provider": "brave",
        "brave_search_api_key": "unit-test-brave-key",
    }
    values.update(overrides)
    return fake_settings(tmp_path, **values)


def _source(url: str, title: str = "Source", snippet: str = "snippet"):
    from copilot.research.provider import WebSource

    return WebSource(title=title, url=url, snippet=snippet, provider="fake")


def _actor(guild_id: int = 500) -> ApprovalActor:
    return ApprovalActor(
        actor_id="7", guild_id=guild_id, kind=ActorKind.human, permissions=ALL
    )


async def _studio(repo: Repository, settings) -> ContentStudio:
    return ContentStudio(repo, ApprovalService(repo), settings=settings, clock=lambda: NOW)


# ---------------------------------------------------------------- URL safety


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/page#section", "https://example.com/page"),
        ("http://example.com", "http://example.com/"),
        ("ftp://example.com/file", None),
        ("javascript:alert(1)", None),
        ("https://user:pass@example.com/", None),
        ("http://localhost/admin", None),
        ("http://127.0.0.1/admin", None),
        ("http://[::1]/admin", None),
        ("http://169.254.169.254/latest/meta-data", None),
        ("https://" + "a" * 1995 + ".com/", None),
        (None, None),
    ],
)
def test_safe_source_url_only_accepts_public_http_urls(raw, expected):
    assert safe_source_url(raw) == expected


# ------------------------------------------------------------- provider parse


def test_brave_results_are_mapped_defensively():
    provider = BraveSearchProvider(api_key="k", timeout_s=1.0, max_results=5)
    payload = {
        "web": {
            "results": [
                {"title": "A", "url": "https://a.example", "description": "one"},
                {"title": "B", "url": "https://b.example"},
                "not-a-dict",
            ]
        }
    }
    rows = provider._results(payload)
    assert [row["url"] for row in rows] == ["https://a.example", "https://b.example"]
    assert rows[0]["snippet"] == "one"


def test_tavily_results_are_mapped_defensively():
    provider = TavilySearchProvider(api_key="k", timeout_s=1.0, max_results=5)
    payload = {"results": [{"title": "A", "url": "https://a.example", "content": "one"}]}
    rows = provider._results(payload)
    assert rows == [{"title": "A", "url": "https://a.example", "snippet": "one"}]
    assert provider._results({"results": "bad"}) == []
    assert provider._results(None) == []


async def test_search_cleans_text_deduplicates_and_bounds_results():
    provider = BraveSearchProvider(api_key="k", timeout_s=1.0, max_results=5)
    payload = {
        "web": {
            "results": [
                {
                    "title": "<b>Reset</b> your password",
                    "url": "https://example.com/reset#top",
                    "description": "Use the <i>reset</i> form &amp; retry",
                },
                {"title": "dup", "url": "https://example.com/reset", "description": "dup"},
                {"title": "private", "url": "http://127.0.0.1/secret", "description": "x"},
                {"title": "", "url": "https://example.org/more", "description": "x"},
            ]
        }
    }

    async def fake_request(query, limit):
        return _Response(payload)

    provider._request = fake_request  # type: ignore[method-assign]
    sources = await provider.search("reset", limit=3)

    assert [source.url for source in sources] == [
        "https://example.com/reset",
        "https://example.org/more",
    ]
    assert sources[0].title == "Reset your password"
    assert sources[0].snippet == "Use the reset form & retry"
    assert sources[1].title == "https://example.org/more"


def test_build_provider_requires_enablement_provider_and_key(tmp_path):
    assert build_web_search_provider(fake_settings(tmp_path)) is None
    assert (
        build_web_search_provider(
            _settings(tmp_path, web_research_enabled=False)
        )
        is None
    )
    assert (
        build_web_search_provider(
            _settings(tmp_path, web_search_provider="brave", brave_search_api_key="")
        )
        is None
    )
    brave = build_web_search_provider(_settings(tmp_path))
    assert isinstance(brave, BraveSearchProvider)
    tavily = build_web_search_provider(
        _settings(
            tmp_path,
            web_search_provider="tavily",
            tavily_api_key="unit-test-tavily-key",
        )
    )
    assert isinstance(tavily, TavilySearchProvider)


# ------------------------------------------------------------------- service


async def test_research_completes_and_persists_guild_scoped_report(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    provider = FakeSearchProvider(
        [_source("https://example.com/a", title="A"), _source("https://example.com/b", title="B")]
    )
    service = ResearchService(
        repo, _settings(tmp_path), provider_factory=lambda _settings: provider
    )

    outcome = await service.research(guild_id="500", query="  <|think|> redis timeouts  ")

    assert outcome.status is ResearchStatus.completed
    assert outcome.ok
    assert outcome.query == "redis timeouts"
    assert outcome.provider == "brave"
    assert outcome.synthesis == "extractive"
    assert len(outcome.sources) == 2
    assert outcome.citation_lines()[0] == "[1] A — https://example.com/a"
    assert provider.calls == [("redis timeouts", 5)]

    rows = await repo.list_research_reports(guild_id="500")
    assert len(rows) == 1
    row = rows[0]
    assert int(row["id"]) == outcome.report_id
    assert str(row["status"]) == "completed"
    assert json.loads(str(row["sources_json"]))[0]["url"] == "https://example.com/a"

    audits = await repo.recent_audit(guild_id="500", limit=10)
    assert [str(item["action"]) for item in audits] == ["research.completed"]
    assert "redis" not in (audits[0]["meta_json"] or "")


async def test_model_synthesis_cannot_invent_urls(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    provider = FakeSearchProvider([_source("https://example.com/real")])
    llm = FakeProvider(
        responses=["Redis timeouts are common [1]. See https://evil.example/fake for details."]
    )
    service = ResearchService(
        repo, _settings(tmp_path), provider_factory=lambda _settings: provider
    )

    outcome = await service.research(
        guild_id="500", query="redis timeouts", llm=llm, mission_id=None
    )

    assert outcome.status is ResearchStatus.completed
    assert outcome.synthesis == "model"
    assert "https://evil.example" not in outcome.summary
    assert "[source]" in outcome.summary
    assert "https://example.com/real" in llm.calls[0]["user"]


async def test_synthesis_failure_falls_back_to_extractive_summary(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    provider = FakeSearchProvider([_source("https://example.com/real", title="Real source")])
    service = ResearchService(
        repo, _settings(tmp_path), provider_factory=lambda _settings: provider
    )

    outcome = await service.research(
        guild_id="500", query="redis timeouts", llm=FakeProvider(responses=[RuntimeError("down")])
    )

    assert outcome.status is ResearchStatus.completed
    assert outcome.synthesis == "extractive"
    assert "Extractive summary from search snippets" in outcome.summary


async def test_unavailable_research_is_reported_not_faked(tmp_path, tmp_db):
    repo = Repository(tmp_db)

    def must_not_build(_settings):
        raise AssertionError("provider must not be built when research is disabled")

    service = ResearchService(
        repo, fake_settings(tmp_path), provider_factory=must_not_build
    )

    outcome = await service.research(guild_id="500", query="anything at all")

    assert outcome.status is ResearchStatus.unavailable
    assert outcome.sources == ()
    assert outcome.reason and "disabled" in outcome.reason
    assert outcome.error_code == "unavailable"
    rows = await repo.list_research_reports(guild_id="500")
    assert len(rows) == 1 and str(rows[0]["status"]) == "unavailable"


@pytest.mark.parametrize(
    ("provider", "error_code"),
    [
        (FakeSearchProvider(error=ResearchProviderError(detail="boom")), "provider_error"),
        (FakeSearchProvider([]), "no_results"),
    ],
)
async def test_research_failures_are_persisted_without_sources(
    tmp_path, tmp_db, provider, error_code
):
    repo = Repository(tmp_db)
    service = ResearchService(
        repo, _settings(tmp_path), provider_factory=lambda _settings: provider
    )

    outcome = await service.research(guild_id="500", query="redis timeouts")

    assert outcome.status is ResearchStatus.failed
    assert outcome.error_code == error_code
    assert outcome.sources == ()
    assert outcome.report_id is not None
    rows = await repo.list_research_reports(guild_id="500")
    assert str(rows[0]["status"]) == "failed"
    assert str(rows[0]["error_code"]) == error_code


async def test_short_query_is_rejected_before_search(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    provider = FakeSearchProvider([])
    service = ResearchService(
        repo, _settings(tmp_path), provider_factory=lambda _settings: provider
    )

    with pytest.raises(ResearchNoResultsError):
        await service.research(guild_id="500", query=" <|think|> ")
    assert provider.calls == []


def test_load_sources_is_defensive():
    assert load_sources("not-json") == []
    assert load_sources("{}") == []
    rows = json.dumps(
        [
            {"title": "ok", "url": "https://example.com", "snippet": "s", "provider": "brave"},
            {"title": "bad", "url": "ftp://example.com"},
            "not-a-dict",
        ]
    )
    loaded = load_sources(rows)
    assert [item.url for item in loaded] == ["https://example.com"]
    assert loaded[0].provider == "brave"


# ----------------------------------------------------------------- provenance


async def test_completed_research_transfers_to_a_draft_with_provenance(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    settings = _settings(tmp_path)
    report_id = await repo.insert_research_report(
        guild_id="500",
        mission_id=None,
        query="redis timeouts",
        provider="brave",
        status="completed",
        summary="Timeouts usually mean saturation [1].",
        synthesis="model",
        sources_json=json.dumps(
            [
                {
                    "title": "Redis latency",
                    "url": "https://example.com/redis",
                    "snippet": "latency",
                    "provider": "brave",
                }
            ]
        ),
        error_code=None,
        created_at=NOW.isoformat(),
    )
    studio = await _studio(repo, settings)

    view = await create_draft_from_research(
        repo,
        studio,
        _actor(),
        report_id=report_id,
        target=ContentTarget(guild_id=500, channel_id=2002, exists=True, can_send=True),
        settings=settings,
    )

    assert "https://example.com/redis" in view.content
    assert "not independently verified" in view.content
    rows = await repo.list_content_drafts(guild_id="500")
    assert len(rows) == 1
    assert str(rows[0]["source_kind"]) == "research"
    assert str(rows[0]["source_ref"]) == f"research:{report_id}"
    audits = await repo.recent_audit(guild_id="500", limit=10)
    assert "research.draft_created" in {str(item["action"]) for item in audits}


async def test_incomplete_or_cross_guild_research_cannot_become_a_draft(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    settings = _settings(tmp_path)
    studio = await _studio(repo, settings)
    failed_id = await repo.insert_research_report(
        guild_id="500",
        mission_id=None,
        query="nothing",
        provider="brave",
        status="failed",
        summary="",
        synthesis="extractive",
        sources_json="[]",
        error_code="no_results",
        created_at=NOW.isoformat(),
    )
    target = ContentTarget(guild_id=500, channel_id=2002, exists=True, can_send=True)

    with pytest.raises(ResearchUnavailableError):
        await create_draft_from_research(
            repo, studio, _actor(), report_id=failed_id, target=target, settings=settings
        )
    with pytest.raises(ResearchReportNotFoundError):
        await create_draft_from_research(
            repo, studio, _actor(999), report_id=failed_id, target=target, settings=settings
        )
    assert (await tmp_db.counts())["content_drafts"] == 0
