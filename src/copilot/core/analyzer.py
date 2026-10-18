"""Analysis orchestration: collect -> (images) -> prompt -> model -> validate -> result.

Model output is never rendered directly: it passes schema validation, source-id
validation (invented message ids are dropped), and a content check. At most one
bounded repair retry is attempted before a friendly failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import OutputValidationError
from ..llm import prompts
from ..llm.base import LLMProvider
from ..llm.schemas import (
    DebugAnalysis,
    ThreadAnalysis,
    analysis_json_schema,
    debug_json_schema,
    parse_debug_analysis,
    parse_thread_analysis,
)
from .collector import CollectedThread, collect_thread
from .images import prepare_images

log = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    analysis: ThreadAnalysis
    collected: CollectedThread
    model: str
    prompt_version: str
    elapsed_s: float
    warnings: list[str] = field(default_factory=list)
    images_analyzed: int = 0
    repair_used: bool = False


@dataclass
class DebugResult:
    analysis: DebugAnalysis
    model: str
    elapsed_s: float
    repair_used: bool = False


def validate_source_ids(analysis: ThreadAnalysis, valid_ids: set[str]) -> tuple[ThreadAnalysis, int]:
    """Drop model-cited message ids that were not actually collected. Returns (analysis, dropped)."""
    dropped = 0
    for field_name in ("key_findings", "decisions", "attempted_solutions", "errors"):
        for item in getattr(analysis, field_name):
            kept = [msg_id for msg_id in item.source_message_ids if msg_id in valid_ids]
            dropped += len(item.source_message_ids) - len(kept)
            item.source_message_ids = kept
    return analysis, dropped


class ThreadAnalyzer:
    def __init__(self, provider: LLMProvider, settings: Any) -> None:
        self._provider = provider
        self._settings = settings

    # -------------------------------------------------------------- summarize

    async def analyze(
        self, thread: Any, *, requested_by: str = "", max_messages: int | None = None
    ) -> AnalysisResult:
        started = time.monotonic()
        collected = await collect_thread(thread, settings=self._settings, max_messages=max_messages)
        warnings = list(collected.skipped_notes)

        images: list[bytes] = []
        candidates = collected.image_attachments()
        if candidates and self._settings.vision_enabled:
            capabilities = await self._provider.capabilities()
            if capabilities.exists and capabilities.supports_vision:
                prep = await prepare_images(
                    candidates,
                    max_images=self._settings.max_images,
                    max_bytes=self._settings.max_image_bytes,
                )
                images = [prepared.data for prepared in prep.images]
                warnings.extend(prep.skipped)
                warnings.extend(prep.notes)
            else:
                warnings.append(
                    f"{len(candidates)} image(s) skipped: the configured model does not support vision"
                )
        elif candidates:
            warnings.append(
                f"{len(candidates)} image(s) skipped: image analysis is disabled in configuration"
            )

        user_prompt = prompts.build_analysis_prompt(collected)
        raw, analysis, repair_used = await self._chat_validated(
            system=prompts.SYSTEM_ANALYSIS,
            user=user_prompt,
            schema=analysis_json_schema(),
            parser=parse_thread_analysis,
            images=images,
        )

        analysis, dropped = validate_source_ids(analysis, collected.message_ids())
        if dropped:
            warnings.append(f"dropped {dropped} message reference(s) that were not part of the thread")

        if not analysis.has_content():
            raise OutputValidationError(
                "The model reply was structurally valid but contained no content.",
                detail=raw[:300],
            )

        return AnalysisResult(
            analysis=analysis,
            collected=collected,
            model=self._settings.ollama_model,
            prompt_version=prompts.PROMPT_VERSION,
            elapsed_s=time.monotonic() - started,
            warnings=warnings,
            images_analyzed=len(images),
            repair_used=repair_used,
        )

    # ------------------------------------------------------------------- debug

    async def explain_error(self, error_text: str) -> DebugResult:
        started = time.monotonic()
        user_prompt = prompts.build_debug_prompt(error_text)
        raw, analysis, repair_used = await self._chat_validated(
            system=prompts.SYSTEM_DEBUG,
            user=user_prompt,
            schema=debug_json_schema(),
            parser=parse_debug_analysis,
        )
        if not analysis.has_content():
            raise OutputValidationError(
                "The model reply was empty.", detail=raw[:300]
            )
        return DebugResult(
            analysis=analysis,
            model=self._settings.ollama_model,
            elapsed_s=time.monotonic() - started,
            repair_used=repair_used,
        )

    # ---------------------------------------------------------------- internal

    async def _chat_validated(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        parser,
        images: list[bytes] | None = None,
    ):
        """One model call + validation, with at most ONE repair retry."""
        try:
            raw = await self._provider.chat(
                system=system, user=user, images=images or [], json_schema=schema
            )
            return raw, parser(raw), False
        except OutputValidationError as first_error:
            log.info(
                "Model output failed validation (%s); attempting one repair",
                type(first_error).__name__,
            )
            repair_prompt = prompts.build_repair_prompt(user, str(first_error))
            raw = await self._provider.chat(
                system=system, user=repair_prompt, images=images or [], json_schema=schema
            )
            return raw, parser(raw), True
