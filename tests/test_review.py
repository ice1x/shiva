import pytest
from faker import Faker

from shiva_agent.review import (
    build_review_prompt,
    filter_files,
    load_enabled_categories,
)

fake = Faker()
Faker.seed(1337)


def make_file(filename=None, patch=None, status="modified"):
    return {
        "filename": filename or fake.file_path(depth=2, extension="py").lstrip("/"),
        "status": status,
        "patch": patch if patch is not None else "@@ -1,2 +1,2 @@\n-" + fake.sentence() + "\n+" + fake.sentence(),
    }


CONFIG = {
    "version": 1,
    "categories": [
        {"id": "logical", "name": "Logical Review", "enabled": True, "prompt": "Examine the logic."},
        {"id": "security", "name": "Security Review", "enabled": True, "prompt": "Examine vulnerabilities."},
        {"id": "code-style", "name": "Code Style Review", "enabled": False, "prompt": "Check style."},
    ],
}


class TestLoadEnabledCategories:
    def test_returns_only_enabled(self):
        cats = load_enabled_categories(CONFIG)
        assert [c["name"] for c in cats] == ["Logical Review", "Security Review"]

    def test_keeps_prompt_text(self):
        cats = load_enabled_categories(CONFIG)
        assert cats[0]["prompt"] == "Examine the logic."

    def test_empty_when_all_disabled(self):
        config = {"categories": [{"id": "x", "name": "X", "enabled": False, "prompt": "p"}]}
        assert load_enabled_categories(config) == []


class TestFilterFiles:
    def test_keeps_all_when_no_extension_filter(self):
        files = [make_file(f"src/{fake.word()}.py"), make_file(f"docs/{fake.word()}.md")]
        assert len(filter_files(files)) == 2

    def test_filters_by_extension(self):
        files = [make_file("app/main.py"), make_file("README.md"), make_file("lib/util.js")]
        kept = filter_files(files, allowed_extensions=[".py", ".js"])
        assert [f["filename"] for f in kept] == ["app/main.py", "lib/util.js"]

    def test_skips_files_without_patch(self):
        binary = {"filename": "logo.png", "status": "added"}  # binary files carry no patch
        kept = filter_files([binary, make_file("a.py")])
        assert [f["filename"] for f in kept] == ["a.py"]

    def test_skips_oversized_patches(self):
        big = make_file("gen/big.py", patch="+x\n" * 10_000)
        small = make_file("a.py")
        kept = filter_files([big, small], max_patch_chars=1_000)
        assert [f["filename"] for f in kept] == ["a.py"]

    def test_skips_removed_files(self):
        removed = make_file("old.py", status="removed")
        kept = filter_files([removed, make_file("new.py")])
        assert [f["filename"] for f in kept] == ["new.py"]


class TestBuildReviewPrompt:
    def test_includes_enabled_category_names_and_prompts(self):
        cats = load_enabled_categories(CONFIG)
        prompt = build_review_prompt(cats, [make_file("a.py")])
        assert "Logical Review" in prompt
        assert "Examine the logic." in prompt
        assert "Security Review" in prompt

    def test_excludes_disabled_categories(self):
        cats = load_enabled_categories(CONFIG)
        prompt = build_review_prompt(cats, [make_file("a.py")])
        assert "Code Style Review" not in prompt
        assert "Check style." not in prompt

    def test_includes_filenames_and_patches(self):
        f = make_file("src/handlers.py", patch="@@ -1 +1 @@\n-old_line\n+new_line")
        prompt = build_review_prompt(load_enabled_categories(CONFIG), [f])
        assert "src/handlers.py" in prompt
        assert "+new_line" in prompt

    def test_empty_files_produces_no_diff_marker(self):
        prompt = build_review_prompt(load_enabled_categories(CONFIG), [])
        assert "No reviewable files" in prompt
