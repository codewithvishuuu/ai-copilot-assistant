"""Model-output pipeline: JSON extraction, bounded validation, status normalization."""

from __future__ import annotations

import json

import pytest

from copilot.errors import EmptyModelOutputError, OutputValidationError
from copilot.llm.schemas import (
    IssueStatus,
    ThreadAnalysis,
    analysis_json_schema,
    debug_json_schema,
    extract_json_object,
    parse_debug_analysis,
    parse_thread_analysis,
)

# ------------------------------------------------------------------- extraction


def test_extract_json_object_finds_first_balanced_object():
    assert extract_json_object('prefix {"a": 1} suffix') == '{"a": 1}'
    assert extract_json_object('{"a": 1} and {"b": 2}') == '{"a": 1}'
    assert extract_json_object('{"outer": {"inner": 2}} tail') == '{"outer": {"inner": 2}}'


def test_extract_json_object_handles_braces_and_escapes_inside_strings():
    assert extract_json_object('{"a": "}"}') == '{"a": "}"}'
    assert extract_json_object('{"a": "escaped \\" quote"}') == '{"a": "escaped \\" quote"}'


def test_extract_json_object_returns_none_without_object():
    assert extract_json_object("") is None
    assert extract_json_object("no braces here") is None
    assert extract_json_object('{"unbalanced": 1') is None
    assert extract_json_object("[1, 2, 3]") is None


# ----------------------------------------------------------------- thread parse


def test_parse_thread_analysis_full_roundtrip():
    payload = {
        "overview": "Sign-in fails for SSO users.",
        "main_problem": "Refresh tokens rejected after the upgrade.",
        "key_findings": [{"text": "Only SSO accounts affected.", "source_message_ids": ["101"]}],
        "decisions": [],
        "attempted_solutions": [{"text": "Clearing cookies did not help."}],
        "unanswered_questions": ["Does rollback fix it?"],
        "errors": [{"text": "invalid_grant", "source_message_ids": ["102"]}],
        "current_status": "in_progress",
        "status_confidence": 0.8,
        "status_reason": "Maintainer is investigating.",
        "suggested_next_step": "Capture the token exchange.",
        "analysis_notes": "Low traffic thread.",
    }
    analysis = parse_thread_analysis(json.dumps(payload))
    assert analysis.overview == "Sign-in fails for SSO users."
    assert analysis.current_status is IssueStatus.in_progress
    assert analysis.status_confidence == 0.8
    assert analysis.key_findings[0].source_message_ids == ["101"]
    assert analysis.error_signatures() == ["invalid_grant"]
    assert analysis.has_content() is True


def test_parse_ignores_unknown_keys_and_missing_fields_default():
    analysis = parse_thread_analysis('{"overview": "x", "evil_key": {"nested": true}}')
    assert analysis.overview == "x"
    assert analysis.current_status is IssueStatus.unclear
    assert analysis.status_confidence == 0.0
    assert analysis.key_findings == []


def test_parse_strips_control_tokens_before_extraction():
    analysis = parse_thread_analysis('<|think|> reasoning... <|end|>{"overview": "ok"}')
    assert analysis.overview == "ok"


def test_parse_rejects_empty_output():
    with pytest.raises(EmptyModelOutputError):
        parse_thread_analysis("")
    with pytest.raises(EmptyModelOutputError):
        parse_thread_analysis("   \n  ")
    with pytest.raises(EmptyModelOutputError):
        parse_thread_analysis("<|think|>")


def test_parse_rejects_output_without_json_object():
    with pytest.raises(OutputValidationError):
        parse_thread_analysis("The thread looks resolved to me.")
    with pytest.raises(OutputValidationError):
        parse_thread_analysis("[1, 2, 3]")


def test_parse_rejects_malformed_json():
    with pytest.raises(OutputValidationError):
        parse_thread_analysis('{"overview": "x",}')


# ------------------------------------------------------- normalization and caps


def test_status_aliases_are_normalized():
    assert parse_thread_analysis('{"current_status": "Open"}').current_status is IssueStatus.unresolved
    assert parse_thread_analysis('{"current_status": "SOLVED"}').current_status is IssueStatus.resolved
    assert parse_thread_analysis('{"current_status": "in progress"}').current_status is IssueStatus.in_progress
    assert parse_thread_analysis('{"current_status": "sausages"}').current_status is IssueStatus.unclear
    assert parse_thread_analysis("{}").current_status is IssueStatus.unclear


def test_confidence_is_clamped_and_coerced():
    assert parse_thread_analysis('{"status_confidence": "0.7"}').status_confidence == 0.7
    assert parse_thread_analysis('{"status_confidence": 5}').status_confidence == 1.0
    assert parse_thread_analysis('{"status_confidence": -1}').status_confidence == 0.0
    assert parse_thread_analysis('{"status_confidence": "abc"}').status_confidence == 0.0


def test_evidence_lists_are_cleaned_truncated_not_failed():
    analysis = parse_thread_analysis(
        json.dumps(
            {
                "key_findings": [
                    {"text": "one", "source_message_ids": ["12", 7, "abc", " 9 ", 3.5]},
                    {"text": ""},
                    "plain string becomes text",
                    {"no_text": True},
                ]
                + [{"text": f"item {n}"} for n in range(10)],
            }
        )
    )
    assert len(analysis.key_findings) == 8  # list capped, no validation error
    assert analysis.key_findings[0].text == "one"
    assert analysis.key_findings[0].source_message_ids == ["12", "7", "9"]
    assert analysis.key_findings[1].text == "plain string becomes text"


def test_evidence_ids_are_capped_at_eight():
    analysis = parse_thread_analysis(
        json.dumps({"errors": [{"text": "boom", "source_message_ids": list(range(12))}]})
    )
    assert analysis.errors[0].source_message_ids == [str(n) for n in range(8)]


def test_evidence_text_is_truncated_to_five_hundred():
    item = parse_thread_analysis(json.dumps({"errors": [{"text": "a" * 900}]})).errors[0]
    assert len(item.text) <= 500
    assert item.text.endswith("[truncated]")


def test_unanswered_questions_normalized_and_capped():
    analysis = parse_thread_analysis(
        json.dumps({"unanswered_questions": ["only one?"]})
    )
    assert analysis.unanswered_questions == ["only one?"]

    analysis = parse_thread_analysis(
        json.dumps({"unanswered_questions": [f"q{n}" + "x" * 400 for n in range(12)]})
    )
    assert len(analysis.unanswered_questions) == 8
    assert all(len(question) <= 300 for question in analysis.unanswered_questions)


def test_non_list_evidence_is_dropped_not_fatal():
    analysis = parse_thread_analysis('{"key_findings": "just a string"}')
    assert analysis.key_findings == []


def test_has_content_and_labels():
    empty = parse_thread_analysis("{}")
    assert empty.has_content() is False
    assert empty.status_label() == "Unclear"
    assert parse_thread_analysis('{"current_status": "resolved"}').status_label() == "Resolved"
    assert parse_thread_analysis('{"suggested_next_step": "do x"}').has_content() is True


def test_error_signatures_are_capped_and_truncated():
    analysis = parse_thread_analysis(
        json.dumps({"errors": [{"text": "E" * 300} for _ in range(7)]})
    )
    signatures = analysis.error_signatures()
    assert len(signatures) == 5
    assert all(len(signature) <= 120 for signature in signatures)


# ------------------------------------------------------------------ debug parse


def test_parse_debug_analysis_roundtrip_and_caps():
    debug = parse_debug_analysis(
        json.dumps(
            {
                "likely_causes": [f"cause {n}" for n in range(9)],
                "next_steps": ["restart", "check logs"],
                "notes": "n" * 900,
            }
        )
    )
    assert len(debug.likely_causes) == 5
    assert debug.next_steps == ["restart", "check logs"]
    assert len(debug.notes) <= 500
    assert debug.has_content() is True
    assert parse_debug_analysis("{}").has_content() is False


def test_parse_debug_analysis_errors_mirror_thread_analysis():
    with pytest.raises(EmptyModelOutputError):
        parse_debug_analysis("")
    with pytest.raises(OutputValidationError):
        parse_debug_analysis("plain prose")


# ------------------------------------------------------------- decoding schemas


def test_analysis_schema_is_flat_and_matches_the_model():
    schema = analysis_json_schema()
    dumped = json.dumps(schema)
    assert "$defs" not in dumped and "$ref" not in dumped

    assert set(schema["required"]) == set(ThreadAnalysis.model_fields)
    status_prop = schema["properties"]["current_status"]
    assert set(status_prop["enum"]) == {status.value for status in IssueStatus}


def test_debug_schema_has_required_fields():
    assert set(debug_json_schema()["required"]) == {"likely_causes", "next_steps", "notes"}
