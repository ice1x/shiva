"""Parity + behaviour tests for review.match_glob.

match_glob is a stdlib-free reimplementation of fnmatch (the n8n Python runner
sandbox forbids importing fnmatch/re). These tests pin it to the real
`fnmatch.fnmatchcase` so it cannot silently drift from fnmatch semantics.
"""

from fnmatch import fnmatchcase

import pytest
from faker import Faker

from shiva_agent.review import match_glob

fake = Faker()

# Patterns that resemble the real exclude globs plus wildcard/class edge cases.
PATTERNS = [
    "*.lock",
    "*.min.js",
    "*.min.*",
    "package-lock.json",
    "*/dist/*",
    "*/vendor/*",
    "?.py",
    "test_[abc].py",
    "file[0-9].txt",
    "no-wildcards",
    "*",
    "**",
    "src/*/mod.py",
    "[!x]bc",
    "a.b.c",
]

NAMES = [
    "yarn.lock",
    "app.min.js",
    "app.min.css",
    "package-lock.json",
    "frontend/dist/bundle.js",
    "node_modules/vendor/x.js",
    "a.py",
    "test_a.py",
    "test_d.py",
    "file7.txt",
    "file10.txt",
    "no-wildcards",
    "src/core/mod.py",
    "xbc",
    "abc",
    "deep/a/b/c/d.min.js",
    "",
]


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("name", NAMES)
def test_matches_fnmatchcase(name, pattern):
    assert match_glob(name, pattern) == fnmatchcase(name, pattern), (name, pattern)


def test_star_crosses_slashes_like_fnmatch():
    # fnmatch's '*' is not path-aware — it spans '/'. Behaviour the exclude
    # filter relies on (a bare '*/dist/*' still matches nested paths).
    assert match_glob("a/b/dist/c/d.js", "*/dist/*") is True


def test_random_paths_match_fnmatchcase():
    for _ in range(200):
        depth = fake.random_int(0, 4)
        parts = [fake.file_name(extension=fake.random_element(["py", "js", "lock", "json"]))
                 if i == depth else fake.word() for i in range(depth + 1)]
        name = "/".join(parts)
        for pat in PATTERNS:
            assert match_glob(name, pat) == fnmatchcase(name, pat), (name, pat)
