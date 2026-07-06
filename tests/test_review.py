import pytest
from faker import Faker

from shiva_agent.review import (
    build_review_prompt,
    filter_files,
    load_enabled_categories,
    merge_config,
    resolve_categories,
    should_skip_pr,
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


def make_pr_payload(draft=False, labels=(), action="opened"):
    return {
        "action": action,
        "pull_request": {
            "number": fake.random_int(min=1, max=999),
            "title": fake.sentence(),
            "draft": draft,
            "labels": [{"name": name} for name in labels],
        },
        "repository": {"full_name": f"{fake.user_name()}/{fake.word()}"},
    }


class TestShouldSkipPr:
    def test_reviews_regular_open_pr(self):
        assert should_skip_pr(make_pr_payload()) is False

    def test_skips_draft_pr(self):
        assert should_skip_pr(make_pr_payload(draft=True)) is True

    def test_skips_pr_labeled_skip_review(self):
        payload = make_pr_payload(labels=[fake.word(), "skip-review"])
        assert should_skip_pr(payload) is True

    def test_reviews_pr_with_other_labels(self):
        payload = make_pr_payload(labels=[fake.word(), fake.word()])
        assert should_skip_pr(payload) is False

    def test_skip_label_is_exact_match(self):
        payload = make_pr_payload(labels=["skip-review-later", "no-skip-review"])
        assert should_skip_pr(payload) is False

    def test_custom_skip_label(self):
        payload = make_pr_payload(labels=["no-bots"])
        assert should_skip_pr(payload, skip_label="no-bots") is True

    @pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "synchronize"])
    def test_reviews_reviewable_actions(self, action):
        assert should_skip_pr(make_pr_payload(action=action)) is False

    @pytest.mark.parametrize("action", ["closed", "labeled", "unlabeled", "edited", "assigned"])
    def test_skips_non_reviewable_actions(self, action):
        # These fire full reviews (paid LLM call + duplicate comment) if not gated.
        assert should_skip_pr(make_pr_payload(action=action)) is True

    def test_skips_ping_and_non_pr_events(self):
        # GitHub's initial webhook 'ping' has no action and no pull_request;
        # without a guard it would render /pulls/undefined/files and 404.
        assert should_skip_pr({"zen": "Keep it simple.", "hook_id": 1}) is True
        assert should_skip_pr({}) is True

    def test_tolerates_missing_fields(self):
        # Reviewable action but degenerate pull_request must not raise.
        assert should_skip_pr({"action": "opened"}) is False
        assert should_skip_pr({"action": "opened", "pull_request": {}}) is False
        assert should_skip_pr(
            {"action": "opened", "pull_request": {"draft": None, "labels": None}}
        ) is False


DEFAULTS = {
    "version": 1,
    "categories": [
        {"id": "structural", "name": "Structural Review", "enabled": True, "prompt": "Check structure."},
        {"id": "logical", "name": "Logical Review", "enabled": True, "prompt": "Check logic."},
        {"id": "code-style", "name": "Code Style Review", "enabled": False, "prompt": "Check style."},
    ],
}


class TestMergeConfig:
    def test_none_override_returns_equivalent_defaults(self):
        merged = merge_config(DEFAULTS, None)
        assert merged["categories"] == DEFAULTS["categories"]

    def test_empty_override_returns_equivalent_defaults(self):
        assert merge_config(DEFAULTS, {})["categories"] == DEFAULTS["categories"]
        assert merge_config(DEFAULTS, {"categories": []})["categories"] == DEFAULTS["categories"]

    def test_override_disables_a_default_category(self):
        override = {"categories": [{"id": "logical", "enabled": False}]}
        merged = merge_config(DEFAULTS, override)
        logical = next(c for c in merged["categories"] if c["id"] == "logical")
        assert logical["enabled"] is False
        # untouched fields survive from the defaults
        assert logical["name"] == "Logical Review"
        assert logical["prompt"] == "Check logic."

    def test_override_enables_a_disabled_category(self):
        override = {"categories": [{"id": "code-style", "enabled": True}]}
        merged = merge_config(DEFAULTS, override)
        assert next(c for c in merged["categories"] if c["id"] == "code-style")["enabled"] is True

    def test_override_replaces_name_and_prompt(self):
        override = {"categories": [{"id": "structural", "name": "Arch", "prompt": "Focus on layering."}]}
        merged = merge_config(DEFAULTS, override)
        structural = next(c for c in merged["categories"] if c["id"] == "structural")
        assert structural["name"] == "Arch"
        assert structural["prompt"] == "Focus on layering."
        assert structural["enabled"] is True  # unspecified field kept

    def test_new_category_is_appended(self):
        override = {"categories": [{"id": "i18n", "name": "i18n Review", "enabled": True, "prompt": "Check strings."}]}
        merged = merge_config(DEFAULTS, override)
        assert [c["id"] for c in merged["categories"]] == ["structural", "logical", "code-style", "i18n"]

    def test_preserves_default_order_and_appends_new_in_override_order(self):
        override = {
            "categories": [
                {"id": "z-custom", "name": "Z", "enabled": True, "prompt": "z"},
                {"id": "logical", "enabled": False},
                {"id": "a-custom", "name": "A", "enabled": True, "prompt": "a"},
            ]
        }
        merged = merge_config(DEFAULTS, override)
        assert [c["id"] for c in merged["categories"]] == [
            "structural",
            "logical",
            "code-style",
            "z-custom",
            "a-custom",
        ]

    def test_keeps_base_version(self):
        assert merge_config(DEFAULTS, {"version": 99})["version"] == 1

    def test_does_not_mutate_inputs(self):
        override = {"categories": [{"id": "logical", "enabled": False}]}
        import copy

        base_before = copy.deepcopy(DEFAULTS)
        override_before = copy.deepcopy(override)
        merge_config(DEFAULTS, override)
        assert DEFAULTS == base_before
        assert override == override_before


class TestResolveCategories:
    def test_without_override_equals_load_enabled(self):
        assert resolve_categories(DEFAULTS) == load_enabled_categories(DEFAULTS)

    def test_override_disable_drops_category(self):
        override = {"categories": [{"id": "logical", "enabled": False}]}
        names = [c["name"] for c in resolve_categories(DEFAULTS, override)]
        assert names == ["Structural Review"]

    def test_override_adds_enabled_custom_category(self):
        override = {"categories": [{"id": "i18n", "name": "i18n Review", "enabled": True, "prompt": "Check strings."}]}
        names = [c["name"] for c in resolve_categories(DEFAULTS, override)]
        assert names == ["Structural Review", "Logical Review", "i18n Review"]


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
