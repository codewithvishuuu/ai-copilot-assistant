"""Bounded web research: configured search providers and grounded synthesis."""

from .provider import (
    BraveSearchProvider,
    TavilySearchProvider,
    WebSearchProvider,
    WebSource,
    build_web_search_provider,
)
from .service import ResearchOutcome, ResearchService, ResearchStatus, create_draft_from_research

__all__ = [
    "BraveSearchProvider",
    "ResearchOutcome",
    "ResearchService",
    "ResearchStatus",
    "TavilySearchProvider",
    "WebSearchProvider",
    "WebSource",
    "build_web_search_provider",
    "create_draft_from_research",
]
