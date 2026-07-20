"""Every relative link in the docs must resolve.

The README was split into docs/ during the Action migration, which is exactly
when relative links rot: a moved file turns `](scripts/x.py)` into a 404 that no
other test notices.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKDOWN = [REPO_ROOT / "README.md"] + sorted((REPO_ROOT / "docs").glob("*.md"))


def links(path):
    """Yield the relative link targets in one markdown file."""
    for chunk in path.read_text().split("](")[1:]:
        target = chunk.split(")")[0].split("#")[0].strip()
        if target and "://" not in target and not target.startswith("mailto:"):
            yield target


@pytest.mark.parametrize("doc", MARKDOWN, ids=lambda p: p.name)
def test_every_relative_link_resolves(doc):
    missing = [t for t in links(doc) if not (doc.parent / t).exists()]
    assert missing == []


def test_the_readme_points_at_both_runtimes():
    text = (REPO_ROOT / "README.md").read_text()
    assert "docs/github-action.md" in text
    assert "docs/n8n.md" in text
