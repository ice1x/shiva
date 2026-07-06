"""Diff filtering and review-prompt assembly for the PR review agent.

The bodies of `filter_files` and `build_review_prompt` are embedded verbatim
into the n8n Code node by scripts/build_workflow.py, so they must stay
dependency-free (standard library only) and self-contained.
"""

DEFAULT_MAX_PATCH_CHARS = 15_000
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

    Convenience wrapper: `load_enabled_categories(merge_config(config, override))`.
    With no override it is equivalent to `load_enabled_categories(config)`.
    """
    return load_enabled_categories(merge_config(config, override))


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


def build_review_prompt(categories, files, conventions=""):
    """Assemble the LLM review prompt from enabled categories and file diffs.

    The prompt is fully specified (task 00012): it lists the review
    categories, optional per-repo `conventions`, a defined severity scale, and
    a fixed output format, so reviews are consistent and machine-skimmable.
    """
    lines = [
        "You are a senior code reviewer. Review the pull request diff below.",
        'Evaluate the changes ONLY against the categories under "Review categories".',
        "",
        "# Review categories",
    ]
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
