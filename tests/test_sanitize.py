"""Tests for post-processing the raw LLM review before it is posted.

Two failure modes seen on real reviews (ice1x/graphbook PR #92) motivate this:

1. the model wrapped its whole answer in a ```markdown fence, which GitHub then
   rendered as a code block instead of a review;
2. findings were anchored to line numbers that do not exist in the diff
   (`file.ts:0`, `file.ts:1`), i.e. invented locations.

`sanitize_review` fixes (1) unconditionally and (2) whenever the reviewed diff
is available to check the citation against.
"""

import pytest
from faker import Faker

from shiva_agent.review import (
    diff_line_index,
    patch_new_lines,
    sanitize_review,
    strip_outer_code_fence,
)

fake = Faker()
Faker.seed(20260720)


# --- strip_outer_code_fence -------------------------------------------------


def test_unwraps_a_markdown_fence_around_the_whole_answer():
    body = "1. **Summary** — a change.\n2. **Verdict** — comment"
    assert strip_outer_code_fence("```markdown\n" + body + "\n```") == body


def test_unwraps_a_bare_fence_around_the_whole_answer():
    body = "1. **Summary** — a change."
    assert strip_outer_code_fence("```\n" + body + "\n```") == body


def test_unwraps_despite_surrounding_blank_lines():
    body = "1. **Summary** — a change."
    assert strip_outer_code_fence("\n\n```md\n" + body + "\n```\n\n") == body


def test_keeps_text_that_is_not_fenced():
    body = "1. **Summary** — a change.\n2. **Verdict** — comment"
    assert strip_outer_code_fence(body) == body


def test_keeps_an_inner_code_block_intact():
    body = "1. **Summary** — a change.\n\n```python\nx = 1\n```\n\nDone."
    assert strip_outer_code_fence(body) == body


def test_unwraps_the_outer_fence_but_preserves_a_nested_code_block():
    inner = "1. **Summary** — a change.\n\n```python\nx = 1\n```"
    assert strip_outer_code_fence("```markdown\n" + inner + "\n```") == inner


def test_does_not_unwrap_when_the_closing_fence_is_missing():
    text = "```markdown\n1. **Summary** — a change."
    assert strip_outer_code_fence(text) == text


def test_does_not_unwrap_two_sibling_code_blocks():
    text = "```python\nx = 1\n```\n\n```python\ny = 2\n```"
    assert strip_outer_code_fence(text) == text


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_blank_input_survives_unchanged(text):
    assert strip_outer_code_fence(text) == text


def test_none_becomes_empty_string():
    assert strip_outer_code_fence(None) == ""


# --- patch_new_lines --------------------------------------------------------

PATCH = """@@ -1,4 +1,6 @@
 import os
+import sys
 
-def old():
+def new():
+    return 1
 # tail
"""


def test_new_line_numbers_cover_added_and_context_lines():
    # New file lines: 1 import os, 2 import sys, 3 blank, 4 def new(),
    # 5 return 1, 6 # tail. The removed `def old()` has no new-file number.
    assert patch_new_lines(PATCH) == {1, 2, 3, 4, 5, 6}


def test_a_second_hunk_starts_at_its_declared_offset():
    patch = "@@ -1,1 +1,1 @@\n context\n@@ -40,2 +50,2 @@\n a\n+b\n"
    assert patch_new_lines(patch) == {1, 50, 51}


def test_a_single_line_hunk_header_without_a_count_is_understood():
    assert patch_new_lines("@@ -7 +7 @@\n+only\n") == {7}


@pytest.mark.parametrize("patch", ["", None, "no hunk header here\n"])
def test_a_patch_without_hunks_has_no_line_numbers(patch):
    assert patch_new_lines(patch) == set()


def test_the_index_maps_every_reviewed_file_to_its_lines():
    files = [
        {"filename": "a.py", "patch": "@@ -1 +1 @@\n+one\n"},
        {"filename": "b.py", "patch": "@@ -1 +10,2 @@\n+ten\n+eleven\n"},
    ]
    assert diff_line_index(files) == {"a.py": {1}, "b.py": {10, 11}}


def test_the_index_of_no_files_is_empty():
    assert diff_line_index([]) == {}


# --- sanitize_review: line anchors ------------------------------------------

FILES = [
    {"filename": "src/app/runtime.ts", "patch": "@@ -1,2 +20,3 @@\n keep\n+added\n+more\n"},
]


def finding(anchor):
    return "3. **Findings**\n   - [medium] Logical Review — " + anchor + " — do the thing"


def test_a_real_line_number_is_kept():
    text = finding("src/app/runtime.ts:21")
    assert sanitize_review(text, FILES) == text


def test_an_invented_line_number_loses_the_anchor_but_keeps_the_path():
    out = sanitize_review(finding("src/app/runtime.ts:0"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_a_line_number_outside_the_diff_loses_the_anchor():
    out = sanitize_review(finding("src/app/runtime.ts:999"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_a_range_inside_the_diff_is_kept():
    text = finding("src/app/runtime.ts:20-22")
    assert sanitize_review(text, FILES) == text


def test_a_range_starting_outside_the_diff_loses_the_anchor():
    out = sanitize_review(finding("src/app/runtime.ts:1-3"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_a_backticked_anchor_is_cleaned_in_place():
    out = sanitize_review(finding("`src/app/runtime.ts:1`"), FILES)
    assert out == finding("`src/app/runtime.ts`")


def test_a_path_not_in_the_diff_keeps_its_text_untouched():
    # Not a file this pass reviewed: leave the prose alone rather than guess.
    text = finding("other/module.py:88")
    assert sanitize_review(text, FILES) == text


def test_without_the_diff_only_the_impossible_zero_anchor_is_dropped():
    assert sanitize_review(finding("src/app/runtime.ts:0")) == finding("src/app/runtime.ts")
    kept = finding("src/app/runtime.ts:21")
    assert sanitize_review(kept) == kept


def test_prose_containing_a_colon_and_digits_is_untouched():
    text = "1. **Summary** — bumps the timeout to 30: it was too short."
    assert sanitize_review(text, FILES) == text


def test_a_time_like_token_is_not_treated_as_an_anchor():
    text = "The retry waits 00:30 before the next attempt."
    assert sanitize_review(text, FILES) == text


def test_fence_and_anchors_are_cleaned_together():
    raw = "```markdown\n" + finding("src/app/runtime.ts:0") + "\n```"
    assert sanitize_review(raw, FILES) == finding("src/app/runtime.ts")


@pytest.mark.parametrize("text", ["", None])
def test_empty_review_sanitizes_to_empty(text):
    assert sanitize_review(text, FILES) == ""


def test_arbitrary_prose_is_never_mangled():
    for _ in range(25):
        text = fake.paragraph(nb_sentences=4)
        assert sanitize_review(text, FILES) == text


def test_a_generated_findings_block_keeps_every_valid_anchor():
    lines = ["3. **Findings**"]
    for line_no in sorted(diff_line_index(FILES)["src/app/runtime.ts"]):
        lines.append(
            "   - [low] Logical Review — src/app/runtime.ts:%d — %s" % (line_no, fake.sentence())
        )
    text = "\n".join(lines)
    assert sanitize_review(text, FILES) == text


# --- the "path:lines 63-29" shape, seen from gpt-4o on a real PR -------------


def test_a_spelled_out_line_anchor_outside_the_diff_is_dropped_whole():
    out = sanitize_review(finding("src/app/runtime.ts:lines 63-29"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_a_spelled_out_singular_line_anchor_is_understood():
    out = sanitize_review(finding("src/app/runtime.ts:line 999"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_a_spelled_out_anchor_inside_the_diff_is_kept():
    text = finding("src/app/runtime.ts:lines 20-22")
    assert sanitize_review(text, FILES) == text


def test_a_backwards_range_is_never_a_valid_citation():
    # 22-20 cites lines that exist, but a reversed range means the model is
    # guessing rather than reading.
    out = sanitize_review(finding("src/app/runtime.ts:22-20"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_a_dangling_lines_keyword_with_no_number_loses_the_anchor():
    out = sanitize_review(finding("src/app/runtime.ts:lines"), FILES)
    assert out == finding("src/app/runtime.ts")


def test_the_word_lines_in_prose_is_untouched():
    text = "The diff moves 30 lines 12 files into place."
    assert sanitize_review(text, FILES) == text
