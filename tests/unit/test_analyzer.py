"""Analyzer: orchestration, validation, source-id checks, bounded repair retry."""

from __future__ import annotations

import io
import json

import pytest
from fakes import FakeAttachment, FakeMessage, FakeProvider, FakeThread, fake_settings
from PIL import Image

from copilot.core.analyzer import ThreadAnalyzer, validate_source_ids
from copilot.errors import OutputValidationError
from copilot.llm.prompts import SYSTEM_ANALYSIS, SYSTEM_DEBUG
from copilot.llm.schemas import ThreadAnalysis


class CountingProvider(FakeProvider):
    """FakeProvider that records capability lookups."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.capability_calls = 0

    async def capabilities(self, *, force_refresh: bool = False):
        self.capability_calls += 1
        return await super().capabilities(force_refresh=force_refresh)


def _valid_payload(**overrides):
    payload = {
        "overview": "Users cannot log in after the upgrade.",
        "main_problem": "Refresh token rejected.",
        "key_findings": [{"text": "Only SSO accounts affected.", "source_message_ids": ["1001"]}],
        "current_status": "in_progress",
        "status_confidence": 0.7,
        "suggested_next_step": "Capture the token exchange.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


def _thread(**kwargs):
    kwargs.setdefault("thread_id", 1001)
    kwargs.setdefault("opening", FakeMessage(id=1001, content="Login broke after the upgrade"))
    kwargs.setdefault("messages", [FakeMessage(id=1002, content="still broken here")])
    return FakeThread(**kwargs)


# ------------------------------------------------------------------- summarize


async def test_analyze_happy_path(tmp_path):
    provider = CountingProvider(responses=[_valid_payload()])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.analyze(_thread())

    assert result.analysis.overview == "Users cannot log in after the upgrade."
    assert result.analysis.current_status.value == "in_progress"
    assert result.model == "gemma4:e2b"
    assert result.prompt_version == "analysis-v1"
    assert result.warnings == []
    assert result.images_analyzed == 0
    assert result.repair_used is False
    assert result.elapsed_s >= 0
    assert provider.capability_calls == 0  # no attachments -> no capability probe

    call = provider.calls[0]
    assert call["system"] == SYSTEM_ANALYSIS
    assert '<thread id="1001"' in call["user"]
    assert "Login broke after the upgrade" in call["user"]
    assert set(call["json_schema"]["required"]) == set(ThreadAnalysis.model_fields)


async def test_analyze_drops_invented_message_references(tmp_path):
    payload = _valid_payload(
        key_findings=[{"text": "cited", "source_message_ids": ["1001", "9999", "424242"]}]
    )
    provider = FakeProvider(responses=[payload])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.analyze(_thread())

    assert result.analysis.key_findings[0].source_message_ids == ["1001"]
    assert "dropped 2 message reference(s) that were not part of the thread" in result.warnings


async def test_analyze_empty_content_fails_without_a_repair(tmp_path):
    provider = FakeProvider(responses=["{}"])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    with pytest.raises(OutputValidationError, match="no content"):
        await analyzer.analyze(_thread())
    assert len(provider.calls) == 1  # repair only retries parse failures


# ----------------------------------------------------------------------- repair


async def test_repair_retry_recovers_from_invalid_json(tmp_path):
    provider = FakeProvider(responses=["Sure! Here is my analysis:", _valid_payload()])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.analyze(_thread())

    assert result.repair_used is True
    assert result.analysis.current_status.value == "in_progress"
    assert len(provider.calls) == 2
    assert provider.calls[1]["user"].startswith(provider.calls[0]["user"])
    assert "ONLY one valid JSON object" in provider.calls[1]["user"]


async def test_repair_retry_also_handles_empty_model_output(tmp_path):
    provider = FakeProvider(responses=["", _valid_payload()])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.analyze(_thread())

    assert result.repair_used is True
    assert len(provider.calls) == 2


async def test_repair_failure_raises_and_stops_after_one_retry(tmp_path):
    provider = FakeProvider(responses=["not json", "still not json"])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    with pytest.raises(OutputValidationError):
        await analyzer.analyze(_thread())
    assert len(provider.calls) == 2  # exactly one repair attempt


# ----------------------------------------------------------------------- images


async def test_analyze_sends_sanitized_images_when_vision_is_available(tmp_path):
    attachment = FakeAttachment(data=_png_bytes(), filename="shot.png")
    message = FakeMessage(id=1002, content="see the screenshot", attachments=[attachment])
    provider = CountingProvider(responses=[_valid_payload()], vision=True)
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.analyze(_thread(messages=[message]))

    assert result.images_analyzed == 1
    assert provider.capability_calls == 1
    images = provider.calls[0]["images"]
    assert len(images) == 1
    assert images[0].startswith(b"\x89PNG")  # re-encoded, ready for the model


async def test_analyze_skips_images_when_the_model_has_no_vision(tmp_path):
    attachment = FakeAttachment(data=_png_bytes(), filename="shot.png")
    message = FakeMessage(id=1002, content="see the screenshot", attachments=[attachment])
    provider = CountingProvider(responses=[_valid_payload()], vision=False)
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.analyze(_thread(messages=[message]))

    assert result.images_analyzed == 0
    assert provider.calls[0]["images"] == []
    assert any("does not support vision" in warning for warning in result.warnings)


async def test_analyze_skips_images_when_vision_is_disabled_in_config(tmp_path):
    attachment = FakeAttachment(data=_png_bytes(), filename="shot.png")
    message = FakeMessage(id=1002, content="see the screenshot", attachments=[attachment])
    provider = CountingProvider(responses=[_valid_payload()])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path, vision_enabled=False))

    result = await analyzer.analyze(_thread(messages=[message]))

    assert result.images_analyzed == 0
    assert provider.capability_calls == 0
    assert any("disabled in configuration" in warning for warning in result.warnings)


# ------------------------------------------------------------------ debug help


async def test_explain_error_returns_debug_result(tmp_path):
    payload = json.dumps({"likely_causes": ["stale token"], "next_steps": ["re-auth"], "notes": "ok"})
    provider = FakeProvider(responses=[payload])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    result = await analyzer.explain_error("Traceback: ValueError at line 3")

    assert result.analysis.likely_causes == ["stale token"]
    assert result.analysis.next_steps == ["re-auth"]
    assert result.model == "gemma4:e2b"
    assert result.repair_used is False
    assert provider.calls[0]["system"] == SYSTEM_DEBUG
    assert "<report>" in provider.calls[0]["user"]


async def test_explain_error_empty_output_fails_without_a_repair(tmp_path):
    provider = FakeProvider(responses=["{}"])
    analyzer = ThreadAnalyzer(provider, fake_settings(tmp_path))

    with pytest.raises(OutputValidationError, match="empty"):
        await analyzer.explain_error("some error")
    assert len(provider.calls) == 1


# ------------------------------------------------------------------- unit bits


def test_validate_source_ids_drops_invented_ids_across_fields():
    analysis = ThreadAnalysis.model_validate(
        {
            "key_findings": [{"text": "a", "source_message_ids": ["1", "999"]}],
            "decisions": [{"text": "b", "source_message_ids": ["888"]}],
            "errors": [{"text": "c", "source_message_ids": ["999", "2"]}],
        }
    )
    cleaned, dropped = validate_source_ids(analysis, {"1", "2"})
    assert dropped == 3
    assert cleaned.key_findings[0].source_message_ids == ["1"]
    assert cleaned.decisions[0].source_message_ids == []
    assert cleaned.errors[0].source_message_ids == ["2"]
