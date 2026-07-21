"""The review prompt carries the accuracy / no-nitpick discipline rules (L0)."""

from shiva_agent.review import build_review_prompt

CATEGORIES = [{"id": "structural", "name": "Structural", "prompt": "check structure"}]


def _prompt():
    return build_review_prompt(CATEGORIES, files=[])


def test_prompt_has_review_discipline_section():
    assert "# Review discipline" in _prompt()


def test_prompt_demands_exact_lines_and_no_guessing():
    p = _prompt()
    assert "exact line numbers" in p
    assert "omit the number rather than guess" in p


def test_prompt_suppresses_nitpicks_and_renames():
    p = _prompt()
    assert "Do NOT raise renames" in p
    assert "micro-optimizations" in p


def test_prompt_prefers_fewer_verifiable_findings():
    assert "wrong or speculative finding is worse than a missed nit" in _prompt()


def test_prompt_forbids_false_missing_claims():
    # The recurring #90/#92 failure: claiming a check/field is missing when it
    # exists (outside the shown diff).
    p = _prompt()
    assert "missing or absent" in p
    assert "do not claim it is missing" in p


def test_prompt_flags_zero_or_one_line_anchors():
    assert "anchored to line 0 or 1" in _prompt()


def test_prompt_forbids_library_framework_swaps():
    assert "swapping" in _prompt() and "test framework" in _prompt()


def test_the_prompt_pins_the_location_format():
    prompt = _prompt()
    assert "`path:line`" in prompt
    assert "start <= end" in prompt


def test_the_prompt_says_bad_anchors_are_stripped():
    # The model is told the post-processing exists, so guessing has no upside.
    assert "stripped before the review is posted" in _prompt()
