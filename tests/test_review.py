import pytest
from faker import Faker

from shiva_agent.review import (
    FETCH_FILE_TOOL_DESCRIPTION,
    FETCH_FILE_TOOL_NAME,
    SEVERITY_LEVELS,
    ConfigError,
    build_agent_system_prompt,
    build_review_prompt,
    filter_files,
    load_enabled_categories,
    merge_config,
    resolve_categories,
    resolve_conventions,
    should_skip_pr,
    split_files_into_batches,
    validate_config,
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


class TestSplitFilesIntoBatches:
    def test_single_batch_when_within_budget(self):
        files = [make_file(patch="x" * 100), make_file(patch="y" * 100)]
        batches = split_files_into_batches(files, max_batch_chars=1_000)
        assert len(batches) == 1
        assert batches[0] == files

    def test_packs_greedily_into_multiple_batches(self):
        a, b, c = (make_file(patch="a" * 60), make_file(patch="b" * 60), make_file(patch="c" * 60))
        batches = split_files_into_batches([a, b, c], max_batch_chars=100)
        # 60 + 60 > 100 → a alone; 60 + 60 > 100 → b alone; c alone
        assert batches == [[a], [b], [c]]

    def test_two_small_files_share_a_batch_then_split(self):
        a, b, c = (make_file(patch="a" * 40), make_file(patch="b" * 40), make_file(patch="c" * 40))
        batches = split_files_into_batches([a, b, c], max_batch_chars=100)
        # 40 + 40 = 80 ≤ 100 → {a, b}; adding c → 120 > 100 → {c}
        assert batches == [[a, b], [c]]

    def test_preserves_file_order(self):
        files = [make_file(f"f{i}.py", patch="p" * 50) for i in range(5)]
        flattened = [f for batch in split_files_into_batches(files, max_batch_chars=120) for f in batch]
        assert flattened == files

    def test_oversized_single_file_gets_its_own_batch(self):
        big = make_file("big.py", patch="z" * 500)
        small = make_file("small.py", patch="s" * 10)
        batches = split_files_into_batches([big, small], max_batch_chars=100)
        # a lone file over budget is never dropped; it just gets its own batch
        assert batches == [[big], [small]]

    def test_empty_input_yields_one_empty_batch(self):
        # one empty batch → the caller still emits a single "no files" review
        assert split_files_into_batches([]) == [[]]

    def test_tolerates_missing_patch(self):
        f = {"filename": "a.py", "status": "modified"}  # no patch key
        assert split_files_into_batches([f], max_batch_chars=100) == [[f]]


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

    def test_rejects_override_custom_category_missing_prompt(self):
        # A hand-written .shiva.yml adds a new category but forgets its prompt.
        override = {"categories": [{"id": "i18n", "name": "i18n Review", "enabled": True}]}
        with pytest.raises(ConfigError) as exc:
            resolve_categories(DEFAULTS, override)
        assert "i18n" in str(exc.value)
        assert "prompt" in str(exc.value)

    def test_rejects_override_non_bool_enabled(self):
        override = {"categories": [{"id": "logical", "enabled": "yes"}]}
        with pytest.raises(ConfigError) as exc:
            resolve_categories(DEFAULTS, override)
        assert "logical" in str(exc.value)
        assert "enabled" in str(exc.value)

    def test_valid_override_passes_through(self):
        override = {"categories": [{"id": "logical", "enabled": False}]}
        assert [c["name"] for c in resolve_categories(DEFAULTS, override)] == ["Structural Review"]

    def test_rejects_structurally_broken_override_before_merge(self):
        # `categories:` written as a mapping (a YAML indentation mistake) would
        # otherwise crash inside merge_config with an opaque AttributeError.
        override = {"categories": {"id": "logical"}}
        with pytest.raises(ConfigError) as exc:
            resolve_categories(DEFAULTS, override)
        assert "categories" in str(exc.value)

    def test_partial_override_may_omit_name_and_prompt(self):
        # An override entry that only flips `enabled` is valid on its own.
        override = {"categories": [{"id": "security", "enabled": True}]}
        validate_config(override, partial=True)  # must not raise

    def test_rejects_config_with_no_enabled_categories(self):
        # 00016: disabling every category would otherwise build a reviewer whose
        # prompt has an empty "# Review categories" section — a do-nothing agent.
        override = {
            "categories": [
                {"id": "structural", "enabled": False},
                {"id": "logical", "enabled": False},
            ]
        }
        with pytest.raises(ConfigError) as exc:
            resolve_categories(DEFAULTS, override)
        assert "enabled" in str(exc.value)


class TestValidateConfig:
    def test_accepts_a_well_formed_config(self):
        # Returns None and does not raise on a valid config.
        assert validate_config(DEFAULTS) is None

    def test_the_shipped_default_config_is_valid(self):
        import yaml
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        config = yaml.safe_load((repo_root / "shiva.config.yml").read_text())
        assert validate_config(config) is None

    def test_config_must_be_a_mapping(self):
        with pytest.raises(ConfigError):
            validate_config(["not", "a", "mapping"])

    def test_categories_must_be_a_list(self):
        with pytest.raises(ConfigError) as exc:
            validate_config({"version": 1, "categories": {"id": "x"}})
        assert "categories" in str(exc.value)
        assert "list" in str(exc.value)

    def test_category_must_be_a_mapping(self):
        with pytest.raises(ConfigError):
            validate_config({"categories": ["logical"]})

    def test_category_requires_non_empty_id(self):
        with pytest.raises(ConfigError) as exc:
            validate_config({"categories": [{"name": "X", "prompt": "p", "enabled": True}]})
        assert "id" in str(exc.value)

    def test_category_id_must_be_unique(self):
        config = {
            "categories": [
                {"id": "dup", "name": "A", "prompt": "a", "enabled": True},
                {"id": "dup", "name": "B", "prompt": "b", "enabled": True},
            ]
        }
        with pytest.raises(ConfigError) as exc:
            validate_config(config)
        assert "dup" in str(exc.value)
        assert "duplicate" in str(exc.value).lower()

    def test_category_requires_non_empty_name(self):
        with pytest.raises(ConfigError) as exc:
            validate_config({"categories": [{"id": "x", "name": "  ", "prompt": "p", "enabled": True}]})
        assert "x" in str(exc.value)
        assert "name" in str(exc.value)

    def test_category_requires_non_empty_prompt(self):
        with pytest.raises(ConfigError) as exc:
            validate_config({"categories": [{"id": "x", "name": "X", "prompt": "", "enabled": True}]})
        assert "x" in str(exc.value)
        assert "prompt" in str(exc.value)

    def test_enabled_must_be_boolean_when_present(self):
        with pytest.raises(ConfigError) as exc:
            validate_config({"categories": [{"id": "x", "name": "X", "prompt": "p", "enabled": 1}]})
        assert "enabled" in str(exc.value)

    def test_enabled_is_optional(self):
        assert validate_config({"categories": [{"id": "x", "name": "X", "prompt": "p"}]}) is None

    def test_conventions_must_be_a_string_when_present(self):
        with pytest.raises(ConfigError) as exc:
            validate_config({"conventions": ["not", "a", "string"], "categories": []})
        assert "conventions" in str(exc.value)

    def test_does_not_require_enabled_categories_by_default(self):
        # Structural validation alone tolerates an all-disabled config; the
        # "at least one enabled" rule is opt-in via require_enabled (00016).
        config = {"categories": [{"id": "x", "name": "X", "prompt": "p", "enabled": False}]}
        assert validate_config(config) is None

    def test_require_enabled_rejects_an_all_disabled_config(self):
        config = {"categories": [{"id": "x", "name": "X", "prompt": "p", "enabled": False}]}
        with pytest.raises(ConfigError) as exc:
            validate_config(config, require_enabled=True)
        assert "enabled" in str(exc.value)

    def test_require_enabled_accepts_at_least_one_enabled(self):
        config = {
            "categories": [
                {"id": "x", "name": "X", "prompt": "p", "enabled": False},
                {"id": "y", "name": "Y", "prompt": "q", "enabled": True},
            ]
        }
        assert validate_config(config, require_enabled=True) is None

    def test_require_enabled_is_relaxed_for_a_partial_override(self):
        # A per-repo override may disable categories; the enabled-count is only
        # enforced on the merged effective config, never on a partial override.
        override = {"categories": [{"id": "logical", "enabled": False}]}
        assert validate_config(override, partial=True, require_enabled=True) is None


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

    def test_defines_every_severity_level(self):
        # 00012: the prompt spells out each severity so the model applies a
        # consistent scale instead of an ad-hoc high/medium/low.
        prompt = build_review_prompt(load_enabled_categories(CONFIG), [make_file("a.py")])
        assert "Severity levels" in prompt
        for level, definition in SEVERITY_LEVELS:
            assert level in prompt
            assert definition in prompt

    def test_specifies_a_structured_output_format(self):
        # 00012: a fixed output shape — summary, verdict, ordered findings.
        prompt = build_review_prompt(load_enabled_categories(CONFIG), [make_file("a.py")])
        assert "Output format" in prompt
        assert "Summary" in prompt
        assert "Verdict" in prompt
        assert "Findings" in prompt

    def test_includes_repo_conventions_when_provided(self):
        # 00012: per-repo conventions are injected so the review respects the
        # target project's house rules.
        conventions = "Prefer pure functions. Never log secrets."
        prompt = build_review_prompt(
            load_enabled_categories(CONFIG), [make_file("a.py")], conventions=conventions
        )
        assert "Repository conventions" in prompt
        assert conventions in prompt

    def test_omits_conventions_section_when_absent(self):
        prompt = build_review_prompt(load_enabled_categories(CONFIG), [make_file("a.py")])
        assert "Repository conventions" not in prompt

    def test_blank_conventions_are_not_rendered(self):
        prompt = build_review_prompt(
            load_enabled_categories(CONFIG), [make_file("a.py")], conventions="   "
        )
        assert "Repository conventions" not in prompt

    def test_marks_the_part_for_a_multi_batch_review(self):
        # 00011: a large PR is reviewed in several passes; each prompt tells the
        # model which part it is so it does not flag the other files as missing.
        prompt = build_review_prompt(
            load_enabled_categories(CONFIG), [make_file("a.py")], part=(2, 3)
        )
        assert "part 2 of 3" in prompt

    def test_no_part_marker_for_a_single_batch(self):
        prompt = build_review_prompt(
            load_enabled_categories(CONFIG), [make_file("a.py")], part=(1, 1)
        )
        assert "part 1 of 1" not in prompt
        assert "review part" not in prompt.lower()

    def test_no_part_marker_when_part_omitted(self):
        prompt = build_review_prompt(load_enabled_categories(CONFIG), [make_file("a.py")])
        assert "review part" not in prompt.lower()


class TestResolveConventions:
    def test_returns_empty_when_neither_provides(self):
        assert resolve_conventions({"version": 1}) == ""

    def test_returns_base_conventions_when_no_override(self):
        assert resolve_conventions({"conventions": "House rules."}) == "House rules."

    def test_override_replaces_base(self):
        merged = resolve_conventions(
            {"conventions": "Base rules."}, {"conventions": "Repo rules."}
        )
        assert merged == "Repo rules."

    def test_falls_back_to_base_when_override_omits(self):
        merged = resolve_conventions({"conventions": "Base rules."}, {"categories": []})
        assert merged == "Base rules."

    def test_strips_surrounding_whitespace(self):
        assert resolve_conventions({"conventions": "\n  Trim me.\n"}) == "Trim me."


class TestBuildAgentSystemPrompt:
    """00013: the AI Agent variant's system prompt tells the model it may fetch
    extra repo files and to review only from evidence it has actually read."""

    def test_references_the_fetch_tool_by_name(self):
        prompt = build_agent_system_prompt()
        assert FETCH_FILE_TOOL_NAME in prompt

    def test_instructs_to_review_from_evidence_not_speculation(self):
        prompt = build_agent_system_prompt().lower()
        # the whole point of giving the model a fetch tool is to stop it guessing
        assert "never" in prompt
        assert "guess" in prompt or "speculat" in prompt

    def test_defers_categories_and_format_to_the_user_message(self):
        # categories/severity/output format still come from build_review_prompt,
        # so the system prompt must not redefine them — it points at the user msg
        prompt = build_agent_system_prompt().lower()
        assert "user message" in prompt

    def test_is_a_nonempty_string(self):
        assert isinstance(build_agent_system_prompt(), str)
        assert build_agent_system_prompt().strip()


class TestFetchFileToolMetadata:
    def test_tool_name_is_a_bare_identifier(self):
        # used verbatim as the n8n tool node name and referenced in the prompt
        assert FETCH_FILE_TOOL_NAME == FETCH_FILE_TOOL_NAME.strip()
        assert " " not in FETCH_FILE_TOOL_NAME

    def test_description_explains_input_and_output(self):
        desc = FETCH_FILE_TOOL_DESCRIPTION.lower()
        assert "path" in desc  # the input the model must supply
        assert "contents" in desc or "text" in desc  # what it returns
