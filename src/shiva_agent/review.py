"""Diff filtering and review-prompt assembly for the PR review agent.

The bodies of `filter_files` and `build_review_prompt` are embedded verbatim
into the n8n Code node by scripts/build_workflow.py, so they must stay
dependency-free (standard library only) and self-contained.
"""

DEFAULT_MAX_PATCH_CHARS = 15_000
# Total patch budget per review pass (task 00011). A large PR is reviewed in
# several passes instead of one oversized prompt: files are packed into batches
# whose combined patch length stays within this budget, and each batch becomes
# its own review. Kept comfortably above DEFAULT_MAX_PATCH_CHARS so a batch can
# still hold a few average files while a single big file gets its own pass.
DEFAULT_MAX_BATCH_CHARS = 45_000
SKIP_REVIEW_LABEL = "skip-review"
# Severity scale the model must apply to every finding (task 00012). Defining
# each level in the prompt keeps ratings consistent across reviews instead of
# leaving high/medium/low to the model's discretion. Ordered most→least severe.
SEVERITY_LEVELS = [
    ("blocker", "must fix before merge — breaks correctness, security, or data integrity"),
    ("high", "should fix before merge — a likely bug or a significant risk"),
    ("medium", "worth fixing — maintainability, a missed edge case, or a minor correctness concern"),
    ("low", "optional polish — style, naming, or clarity"),
]
# pull_request actions that carry a diff worth reviewing. Everything else
# (closed/labeled/edited/..., and non-PR deliveries like the initial 'ping')
# is skipped so we neither 404 on a missing PR number nor pay for a duplicate
# review on every label change.
REVIEWABLE_ACTIONS = frozenset({"opened", "reopened", "ready_for_review", "synchronize"})

# Name of the tool the AI Agent variant exposes so the model can pull extra
# repository files for context (task 00013). Referenced both in the agent's
# system prompt (below) and as the generated n8n tool node's name, so the two
# never drift.
FETCH_FILE_TOOL_NAME = "fetch_repo_file"
# Tool description shown to the model. It fetches the full current contents of a
# file from the pull request's head commit, so the reviewer can see code the
# diff only hints at (the rest of a partially-shown file, a caller, a callee, a
# base class, a referenced module) instead of guessing.
FETCH_FILE_TOOL_DESCRIPTION = (
    "Fetch the full current contents of a file from the pull request's "
    "repository at the head commit. Input: a repository-relative file path "
    "(for example src/app/main.py). Returns the file's text, or an error if "
    "the path does not exist. Use it to read context the diff does not show: "
    "the rest of a file when only a hunk is included, a caller or callee, a "
    "base class, a configuration file, or a referenced module."
)


def build_agent_system_prompt():
    """Return the system prompt for the AI Agent review variant (task 00013).

    The default workflow sends the diff to the model in a single stateless HTTP
    call. The agent variant instead runs the model in a tool-use loop with a
    `fetch_repo_file` tool, so it can request additional files from the target
    repository when the diff alone is not enough to judge a change. This system
    prompt tells the model when to reach for that tool and — crucially — to base
    every finding on code it has actually read rather than on speculation about
    code it has not seen. The concrete review categories, severity scale, and
    output format still arrive in the user message from `build_review_prompt`,
    so the two variants produce reviews in the same shape.
    """
    return (
        "You are a senior code reviewer. You are reviewing a pull request and "
        "have a tool for extra context.\n"
        f"When the diff alone is not enough to judge a change, call the "
        f"`{FETCH_FILE_TOOL_NAME}` tool to fetch the full current contents of a "
        "file from the pull request's repository (pass a repository-relative "
        "path). Use it to read the rest of a file when only a hunk is shown, or "
        "to inspect a caller, a callee, a base class, or a referenced module.\n"
        "Fetch only files that materially help the review, and do not fetch "
        "more than a handful. Base every finding on evidence you have actually "
        "read — the diff or a file you fetched — never on a guess about code you "
        "have not seen.\n"
        "Then produce the review exactly as instructed in the user message: the "
        "same categories, severity scale, and output format."
    )


def should_skip_pr(payload, skip_label=SKIP_REVIEW_LABEL):
    """Return True when the webhook event must not be reviewed.

    Skips non-reviewable actions and non-PR events (the `action` is absent or
    not in REVIEWABLE_ACTIONS), draft PRs, and PRs carrying the opt-out label.
    `payload` is the GitHub webhook event body.
    """
    if payload.get("action") not in REVIEWABLE_ACTIONS:
        return True
    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return True
    labels = pr.get("labels") or []
    return any(label.get("name") == skip_label for label in labels)


def load_enabled_categories(config):
    """Return [{'name', 'prompt'}, ...] for categories with enabled: true."""
    return [
        {"name": c["name"], "prompt": c["prompt"].strip()}
        for c in config.get("categories", [])
        if c.get("enabled")
    ]


class ConfigError(ValueError):
    """Raised when a review config (`shiva.config.yml` / a repo's `.shiva.yml`) is malformed."""


def validate_config(config, partial=False):
    """Validate a review config, raising ConfigError with a clear message.

    Per-repo overrides (task 00014) invite users to hand-write a `.shiva.yml`, so
    a typo — an enabled category with no `prompt`, `enabled: "yes"` as a string, a
    duplicated `id` — must fail the build with an actionable message instead of a
    bare `KeyError` deep inside `load_enabled_categories` (task 00015).

    Checks: `config` is a mapping; `conventions` (if present) is a string;
    `categories` is a list of mappings; each category has a non-empty string
    `id` (unique across the list), a non-empty string `name` and `prompt`, and
    an `enabled` that is a bool when present. Returns None on success.

    With `partial=True` the config is treated as a per-repo override: `name` and
    `prompt` become optional (an override entry may set only `enabled`), though
    when present they must still be non-empty strings. Everything else — the
    structural shape, the `id`, uniqueness, the `enabled` type — is checked the
    same way, so a malformed override is caught before it reaches `merge_config`.
    The *effective* (merged) config is then validated in full, so a category
    that ends up without a `name`/`prompt` is still rejected.
    """
    if not isinstance(config, dict):
        raise ConfigError(f"config must be a mapping, got {type(config).__name__}")

    conventions = config.get("conventions")
    if conventions is not None and not isinstance(conventions, str):
        raise ConfigError(
            f"conventions must be a string, got {type(conventions).__name__}"
        )

    categories = config.get("categories", [])
    if not isinstance(categories, list):
        raise ConfigError(
            f"categories must be a list, got {type(categories).__name__}"
        )

    seen_ids = set()
    for i, cat in enumerate(categories):
        where = f"category #{i + 1}"
        if not isinstance(cat, dict):
            raise ConfigError(f"{where} must be a mapping, got {type(cat).__name__}")

        cat_id = cat.get("id")
        if not isinstance(cat_id, str) or not cat_id.strip():
            raise ConfigError(f"{where} is missing a non-empty string 'id'")
        if cat_id in seen_ids:
            raise ConfigError(f"category '{cat_id}' has a duplicate 'id'")
        seen_ids.add(cat_id)

        for field in ("name", "prompt"):
            value = cat.get(field)
            if value is None and partial:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    f"category '{cat_id}' is missing a non-empty string '{field}'"
                )

        enabled = cat.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(
                f"category '{cat_id}' has a non-boolean 'enabled' "
                f"({type(enabled).__name__}); use true or false"
            )


def merge_config(base, override):
    """Merge a per-repo `override` config over the `base` defaults by category `id`.

    A target repository ships its own `.shiva.yml` to customize the review
    (task 00014). Merge rules, applied by category `id`:

    - a default whose `id` also appears in the override keeps its fields except
      the ones the override provides (so `{'id': 'x', 'enabled': false}` just
      flips the flag and leaves name/prompt intact);
    - a category whose `id` is not among the defaults is appended as a new,
      first-class custom category, in override order;
    - the defaults' relative order is preserved; the top-level `version` is the
      schema version and always comes from `base`.

    Neither input is mutated. `override` may be None or lack a `categories` key,
    in which case the result is an independent copy of the defaults.
    """
    overrides_by_id = {}
    extra = []
    for cat in (override or {}).get("categories") or []:
        cat_id = cat.get("id")
        if cat_id is not None and any(b.get("id") == cat_id for b in base.get("categories", [])):
            overrides_by_id[cat_id] = cat
        else:
            extra.append(dict(cat))

    merged_categories = []
    for cat in base.get("categories", []):
        merged = dict(cat)
        merged.update(overrides_by_id.get(cat.get("id"), {}))
        merged_categories.append(merged)
    merged_categories.extend(extra)

    return {"version": base.get("version"), "categories": merged_categories}


def resolve_categories(config, override=None):
    """Return the enabled categories after applying an optional per-repo override.

    Convenience wrapper: `load_enabled_categories(merge_config(config, override))`,
    with the merged (effective) config validated first (task 00015) so a
    malformed `.shiva.yml` fails with a clear ConfigError instead of a bare
    KeyError. With no override it is equivalent to
    `load_enabled_categories(config)` on a valid config.
    """
    if override is not None:
        validate_config(override, partial=True)
    merged = merge_config(config, override)
    validate_config(merged)
    return load_enabled_categories(merged)


def resolve_conventions(config, override=None):
    """Return the repo conventions text after applying an optional override.

    Repo conventions are free-form house rules injected into the review prompt
    (task 00012) so the LLM respects the target project's standards. The
    `override` (a target repo's `.shiva.yml`) wins over the base `config` when
    it provides a non-empty `conventions`; otherwise the base value is used.
    Returns "" (with surrounding whitespace stripped) when neither provides any.
    """
    for src in (override, config):
        conventions = (src or {}).get("conventions")
        if conventions and conventions.strip():
            return conventions.strip()
    return ""


def filter_files(files, allowed_extensions=None, max_patch_chars=DEFAULT_MAX_PATCH_CHARS):
    """Filter GitHub /pulls/{n}/files items down to reviewable ones.

    Drops files without a patch (binary), removed files, oversized patches,
    and — when allowed_extensions is given — files with other extensions.
    """
    kept = []
    for f in files:
        patch = f.get("patch")
        if not patch:
            continue
        if f.get("status") == "removed":
            continue
        if len(patch) > max_patch_chars:
            continue
        if allowed_extensions is not None:
            name = f.get("filename", "")
            if not any(name.endswith(ext) for ext in allowed_extensions):
                continue
        kept.append(f)
    return kept


def split_files_into_batches(files, max_batch_chars=DEFAULT_MAX_BATCH_CHARS):
    """Group filtered files into review batches bounded by total patch size.

    Large PRs are reviewed in several passes rather than one giant prompt
    (task 00011): files are packed greedily, in their given order, into batches
    whose combined patch length stays within `max_batch_chars`. A file whose
    own patch already exceeds the budget still gets its own (over-budget) batch
    instead of being dropped — filter_files has already capped any single patch
    at DEFAULT_MAX_PATCH_CHARS, so each pass stays bounded.

    Returns a list of file-lists. An empty input yields a single empty batch,
    so the caller still emits one "no reviewable files" review.
    """
    batches = []
    current = []
    current_chars = 0
    for f in files:
        size = len(f.get("patch") or "")
        if current and current_chars + size > max_batch_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(f)
        current_chars += size
    batches.append(current)  # always emit the last (possibly empty) batch
    return batches


def build_review_prompt(categories, files, conventions="", part=None):
    """Assemble the LLM review prompt from enabled categories and file diffs.

    The prompt is fully specified (task 00012): it lists the review
    categories, optional per-repo `conventions`, a defined severity scale, and
    a fixed output format, so reviews are consistent and machine-skimmable.

    `part` is an optional ``(index, count)`` for large PRs reviewed in several
    passes (task 00011); when ``count > 1`` the prompt states which part this
    is so the model scopes its review to the files shown instead of flagging
    the split-off files as missing.
    """
    lines = [
        "You are a senior code reviewer. Review the pull request diff below.",
        'Evaluate the changes ONLY against the categories under "Review categories".',
        "",
    ]
    if part is not None:
        index, count = part
        if count > 1:
            lines.append(
                f"This is review part {index} of {count} for a large pull request; "
                "review only the diff below and scope every finding to these files."
            )
            lines.append("")
    lines.append("# Review categories")
    for c in categories:
        lines.append("## " + c["name"])
        lines.append(c["prompt"])
        lines.append("")

    if conventions and conventions.strip():
        lines.append("# Repository conventions")
        lines.append("Honour the target repository's house rules while reviewing:")
        lines.append("")
        lines.append(conventions.strip())
        lines.append("")

    lines.append("# Severity levels")
    lines.append("Rate every finding with exactly one severity:")
    for level, definition in SEVERITY_LEVELS:
        lines.append("- **" + level + "** — " + definition + ".")
    lines.append("")

    lines.append("# Output format")
    lines.append("Respond in GitHub-flavored markdown with this exact structure:")
    lines.append("1. **Summary** — one sentence describing what the PR changes.")
    lines.append(
        "2. **Verdict** — exactly one of `approve`, `comment`, or `request changes`."
    )
    lines.append(
        "3. **Findings** — a bullet per issue, ordered by severity (blocker first), "
        "each in the shape: `[severity] category — path:lines — issue, then the concrete fix`."
    )
    lines.append(
        "Report only issues that fall under the categories above. "
        "If there are no findings, write `No issues found.` under Findings."
    )
    lines.append("")

    lines.append("# Diff")
    if not files:
        lines.append("No reviewable files in this pull request.")
    for f in files:
        lines.append("## " + f.get("filename", "<unknown>") + " (" + f.get("status", "modified") + ")")
        lines.append("```diff")
        lines.append(f.get("patch", ""))
        lines.append("```")
    return "\n".join(lines)
