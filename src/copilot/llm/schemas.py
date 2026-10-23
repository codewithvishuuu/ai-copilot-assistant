"""Pydantic models and robust JSON extraction for all model outputs.

Reliability pipeline (see AI_SYSTEM.md):
1. schema-constrained decoding when the server supports it (fallback handled in the provider),
2. control-token/thought stripping + first balanced JSON object extraction,
3. Pydantic validation with bounded fields (lists truncated instead of failing),
4. empty output -> EmptyModelOutputError, invalid output -> OutputValidationError
   (the analyzer allows at most one repair retry).
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..errors import EmptyModelOutputError, OutputValidationError
from ..util.text import strip_control_tokens, truncate

log = logging.getLogger(__name__)


class IssueStatus(str, Enum):
    unresolved = "unresolved"
    in_progress = "in_progress"
    waiting_for_user = "waiting_for_user"
    resolved = "resolved"
    unclear = "unclear"


STATUS_LABELS: dict[str, str] = {
    "unresolved": "Unresolved",
    "in_progress": "In progress",
    "waiting_for_user": "Waiting for user",
    "resolved": "Resolved",
    "unclear": "Unclear",
}

# Common ways small models spell the statuses; normalized before validation.
_STATUS_ALIASES = {
    "unresolved": "unresolved",
    "open": "unresolved",
    "not_resolved": "unresolved",
    "notresolved": "unresolved",
    "in_progress": "in_progress",
    "inprogress": "in_progress",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "working": "in_progress",
    "waiting_for_user": "waiting_for_user",
    "waiting for user": "waiting_for_user",
    "waiting": "waiting_for_user",
    "needs_info": "waiting_for_user",
    "resolved": "resolved",
    "solved": "resolved",
    "fixed": "resolved",
    "done": "resolved",
    "unclear": "unclear",
    "unknown": "unclear",
    "unclear/unknown": "unclear",
}


def _clean_str(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return truncate(strip_control_tokens(value).strip(), limit)


def _clean_str_list(value: Any, *, limit_items: int, limit_chars: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_str(item, limit_chars)
        if text:
            out.append(text)
        if len(out) >= limit_items:
            break
    return out


def _clean_evidence_list(value: Any, *, limit_items: int = 8, limit_chars: int = 500) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    for item in value:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = _clean_str(item.get("text"), limit_chars)
        if not text:
            continue
        ids_raw = item.get("source_message_ids")
        if isinstance(ids_raw, (str, int)):
            ids_raw = [ids_raw]
        ids: list[str] = []
        if isinstance(ids_raw, (list, tuple)):
            for cand in ids_raw:
                cand_str = str(cand).strip()
                if cand_str.isdigit():
                    ids.append(cand_str)
                if len(ids) >= 8:
                    break
        out.append({"text": text, "source_message_ids": ids})
        if len(out) >= limit_items:
            break
    return out


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = ""
    source_message_ids: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean_str(value, 500)

    @field_validator("source_message_ids", mode="before")
    @classmethod
    def _clean_ids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, int)):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        ids: list[str] = []
        for cand in value:
            cand_str = str(cand).strip()
            if cand_str.isdigit():
                ids.append(cand_str)
            if len(ids) >= 8:
                break
        return ids


class ThreadAnalysis(BaseModel):
    """Validated structured summary of one thread. All fields bounded."""

    model_config = ConfigDict(extra="ignore")

    overview: str = ""
    main_problem: str = ""
    key_findings: list[EvidenceItem] = Field(default_factory=list)
    decisions: list[EvidenceItem] = Field(default_factory=list)
    attempted_solutions: list[EvidenceItem] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    errors: list[EvidenceItem] = Field(default_factory=list)
    current_status: IssueStatus = IssueStatus.unclear
    status_confidence: float = 0.0
    status_reason: str = ""
    suggested_next_step: str = ""
    analysis_notes: str = ""

    @field_validator("overview", mode="before")
    @classmethod
    def _v_overview(cls, value: Any) -> str:
        return _clean_str(value, 1500)

    @field_validator("main_problem", mode="before")
    @classmethod
    def _v_problem(cls, value: Any) -> str:
        return _clean_str(value, 1000)

    @field_validator("status_reason", mode="before")
    @classmethod
    def _v_reason(cls, value: Any) -> str:
        return _clean_str(value, 500)

    @field_validator("suggested_next_step", mode="before")
    @classmethod
    def _v_next(cls, value: Any) -> str:
        return _clean_str(value, 800)

    @field_validator("analysis_notes", mode="before")
    @classmethod
    def _v_notes(cls, value: Any) -> str:
        return _clean_str(value, 500)

    @field_validator("current_status", mode="before")
    @classmethod
    def _v_status(cls, value: Any) -> str:
        if value is None:
            return "unclear"
        key = str(value).strip().lower()
        return _STATUS_ALIASES.get(key, "unclear")

    @field_validator("status_confidence", mode="before")
    @classmethod
    def _v_confidence(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, number))

    @field_validator("key_findings", "decisions", "attempted_solutions", "errors", mode="before")
    @classmethod
    def _v_evidence(cls, value: Any) -> list[dict]:
        return _clean_evidence_list(value)

    @field_validator("unanswered_questions", mode="before")
    @classmethod
    def _v_questions(cls, value: Any) -> list[str]:
        return _clean_str_list(value, limit_items=8, limit_chars=300)

    # ------------------------------------------------------------------ helpers

    def has_content(self) -> bool:
        return bool(
            self.overview
            or self.main_problem
            or self.key_findings
            or self.decisions
            or self.attempted_solutions
            or self.unanswered_questions
            or self.errors
            or self.suggested_next_step
        )

    def status_label(self) -> str:
        return STATUS_LABELS.get(self.current_status.value, "Unclear")

    def error_signatures(self, limit: int = 5) -> list[str]:
        return [truncate(item.text, 120) for item in self.errors[:limit]]


class DebugAnalysis(BaseModel):
    """Validated structured reply for /debug (never executes anything)."""

    model_config = ConfigDict(extra="ignore")

    likely_causes: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("likely_causes", "next_steps", mode="before")
    @classmethod
    def _v_lists(cls, value: Any) -> list[str]:
        return _clean_str_list(value, limit_items=5, limit_chars=300)

    @field_validator("notes", mode="before")
    @classmethod
    def _v_notes(cls, value: Any) -> str:
        return _clean_str(value, 500)

    def has_content(self) -> bool:
        return bool(self.likely_causes or self.next_steps or self.notes)


class ContentDraftOutput(BaseModel):
    """Model-generated draft text, plus explicit research/warning signals."""

    model_config = ConfigDict(extra="ignore")

    content: str = ""
    needs_research: bool = False
    research_notes: str = ""
    warnings: list[str] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def _v_content(cls, value: Any) -> str:
        # Keep line breaks (Discord formatting) but strip model control tokens.
        if value is None:
            return ""
        return strip_control_tokens(str(value)).strip()[:8000]

    @field_validator("needs_research", mode="before")
    @classmethod
    def _v_needs_research(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "required"}
        return bool(value)

    @field_validator("research_notes", mode="before")
    @classmethod
    def _v_research_notes(cls, value: Any) -> str:
        return _clean_str(value, 500)

    @field_validator("warnings", mode="before")
    @classmethod
    def _v_warnings(cls, value: Any) -> list[str]:
        return _clean_str_list(value, limit_items=5, limit_chars=300)

    def has_content(self) -> bool:
        return bool(self.content.strip())


# ---------------------------------------------------------------- extraction


def extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` object in ``text``, or None.

    Handles strings and escapes while scanning, so braces inside string values
    do not confuse the balance counter.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _load_object(raw: str) -> dict:
    cleaned = strip_control_tokens(raw or "")
    if not cleaned.strip():
        raise EmptyModelOutputError()
    candidate = extract_json_object(cleaned)
    if candidate is None:
        raise OutputValidationError(
            "The model reply did not contain a JSON object.", detail=cleaned[:400]
        )
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise OutputValidationError(
            f"The model reply was not valid JSON ({exc.msg}).", detail=candidate[:400]
        ) from exc
    if not isinstance(data, dict):
        raise OutputValidationError("The model reply was not a JSON object.", detail=candidate[:400])
    return data


def parse_thread_analysis(raw: str) -> ThreadAnalysis:
    data = _load_object(raw)
    try:
        return ThreadAnalysis.model_validate(data)
    except ValidationError as exc:
        raise OutputValidationError(
            "The model reply failed schema validation.", detail=str(exc)[:600]
        ) from exc


def parse_debug_analysis(raw: str) -> DebugAnalysis:
    data = _load_object(raw)
    try:
        return DebugAnalysis.model_validate(data)
    except ValidationError as exc:
        raise OutputValidationError(
            "The model reply failed schema validation.", detail=str(exc)[:600]
        ) from exc


def parse_content_draft(raw: str) -> ContentDraftOutput:
    data = _load_object(raw)
    try:
        return ContentDraftOutput.model_validate(data)
    except ValidationError as exc:
        raise OutputValidationError("The model reply failed schema validation.", detail=str(exc)[:600]) from exc


# ------------------------------------------------------- decoding schemas


def _evidence_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "source_message_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text"],
    }


def analysis_json_schema() -> dict:
    """Flattened JSON schema (no $defs/$ref) for Ollama's schema-constrained decoding."""
    evidence = _evidence_schema()
    return {
        "type": "object",
        "properties": {
            "overview": {"type": "string"},
            "main_problem": {"type": "string"},
            "key_findings": {"type": "array", "items": evidence},
            "decisions": {"type": "array", "items": evidence},
            "attempted_solutions": {"type": "array", "items": evidence},
            "unanswered_questions": {"type": "array", "items": {"type": "string"}},
            "errors": {"type": "array", "items": evidence},
            "current_status": {"type": "string", "enum": [s.value for s in IssueStatus]},
            "status_confidence": {"type": "number"},
            "status_reason": {"type": "string"},
            "suggested_next_step": {"type": "string"},
            "analysis_notes": {"type": "string"},
        },
        "required": [
            "overview",
            "main_problem",
            "key_findings",
            "decisions",
            "attempted_solutions",
            "unanswered_questions",
            "errors",
            "current_status",
            "status_confidence",
            "status_reason",
            "suggested_next_step",
            "analysis_notes",
        ],
    }


def debug_json_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "likely_causes": {"type": "array", "items": {"type": "string"}},
            "next_steps": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
        },
        "required": ["likely_causes", "next_steps", "notes"],
    }


def content_draft_json_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "needs_research": {"type": "boolean"},
            "research_notes": {"type": "string"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["content", "needs_research", "research_notes", "warnings"],
    }
