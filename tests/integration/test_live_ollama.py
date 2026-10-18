"""Opt-in live test against a real local Ollama server.

Skipped unless ``RUN_LIVE_OLLAMA=1`` is set, and the configured model must be
installed (see CONFIGURATION.md). This is the only test that talks to a real
model and a real server — everything else in the suite is mock-only.
"""

from __future__ import annotations

import os

import pytest

from copilot.config import Settings
from copilot.llm.ollama_provider import OllamaProvider
from copilot.llm.schemas import analysis_json_schema, parse_thread_analysis

pytestmark = [
    pytest.mark.live_ollama,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_OLLAMA") != "1",
        reason="live Ollama test: set RUN_LIVE_OLLAMA=1 to run",
    ),
]


async def test_live_server_is_reachable_and_model_installed():
    settings = Settings(_env_file=None)
    provider = OllamaProvider(settings)
    try:
        health = await provider.health()
        assert health.reachable, f"Ollama not reachable at {settings.ollama_base_url}: {health.error}"

        capabilities = await provider.capabilities(force_refresh=True)
        assert capabilities.exists, f"model {settings.ollama_model} not installed: {capabilities.error}"
    finally:
        await provider.aclose()


async def test_live_json_analysis_roundtrip_parses():
    settings = Settings(_env_file=None)
    provider = OllamaProvider(settings)
    try:
        raw = await provider.chat(
            system=(
                "You are a JSON-only test assistant. Reply with exactly one JSON object "
                "and nothing else."
            ),
            user=(
                'Reply with {"overview": "hello from the live test", '
                '"current_status": "unclear", "status_confidence": 0.5}.'
            ),
            json_schema=analysis_json_schema(),
            timeout_s=180.0,
        )

        analysis = parse_thread_analysis(raw)
        assert analysis.has_content()
    finally:
        await provider.aclose()
