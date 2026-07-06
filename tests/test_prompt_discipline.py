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
