"""Skip-empty-review gate: decide post/skip from the single LLM response."""

import pytest

from shiva_agent.review import (
    NO_FINDINGS_SENTINEL,
    extract_review_text,
    review_has_action_items,
)

TAGGED = "1. **Summary** — x\n2. **Verdict** — comment\n3. **Findings**\n- [high] security — a.py:3 — bug"


# --- review_has_action_items ----------------------------------------------


@pytest.mark.parametrize("clear", ["APPROVED", "approved", " APPROVED ", "APPROVED.", "APPROVED\n"])
def test_bare_sentinel_has_no_action_items(clear):
    assert review_has_action_items(clear) is False


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_empty_has_no_action_items(empty):
    assert review_has_action_items(empty) is False


def test_severity_tagged_finding_is_actionable():
    assert review_has_action_items(TAGGED) is True


@pytest.mark.parametrize("level", ["blocker", "high", "medium", "low"])
def test_each_severity_tag_counts(level):
    assert review_has_action_items(f"- [{level}] cat — f:1 — issue") is True


def test_request_changes_without_tags_is_actionable():
    assert review_has_action_items("Verdict: request changes. Please fix the auth.") is True


def test_unexpected_untagged_text_is_suppressed():
    # No tag, not a request-changes verdict, not the sentinel → err toward silence.
    assert review_has_action_items("Looks basically fine, minor thoughts.") is False


def test_sentinel_constant_is_uppercase_word():
    assert NO_FINDINGS_SENTINEL == "APPROVED"


# --- extract_review_text (per wire protocol) ------------------------------


def test_extract_openai():
    resp = {"choices": [{"message": {"content": "the review"}}]}
    assert extract_review_text(resp, "openai") == "the review"


def test_extract_anthropic_joins_text_blocks_only():
    resp = {"content": [
        {"type": "thinking", "text": "ignore"},
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]}
    # Anthropic mirror skips non-text blocks.
    assert extract_review_text(resp, "anthropic") == "ab"


@pytest.mark.parametrize("resp", [{}, {"choices": []}, None])
def test_extract_openai_empty_shapes(resp):
    assert extract_review_text(resp, "openai") == ""


def test_gate_end_to_end_openai_sentinel_skips():
    resp = {"choices": [{"message": {"content": "APPROVED"}}]}
    assert review_has_action_items(extract_review_text(resp, "openai")) is False


def test_gate_end_to_end_openai_findings_posts():
    resp = {"choices": [{"message": {"content": TAGGED}}]}
    assert review_has_action_items(extract_review_text(resp, "openai")) is True
