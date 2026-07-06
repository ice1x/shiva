"""Diff filtering and review-prompt assembly for the PR review agent.

The bodies of `filter_files` and `build_review_prompt` are embedded verbatim
into the n8n Code node by scripts/build_workflow.py, so they must stay
dependency-free (standard library only) and self-contained.
"""

DEFAULT_MAX_PATCH_CHARS = 15_000
SKIP_REVIEW_LABEL = "skip-review"
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


def build_review_prompt(categories, files):
    """Assemble the LLM review prompt from enabled categories and file diffs."""
    lines = [
        "You are a code review agent. Review the pull request diff below.",
        "Evaluate the changes ONLY against the following review categories:",
        "",
    ]
    for c in categories:
        lines.append("## " + c["name"])
        lines.append(c["prompt"])
        lines.append("")
    lines.append(
        "For each finding, state the category, the file, the relevant lines, "
        "a severity (high/medium/low), and a short actionable explanation. "
        "If a category has no findings, say so in one line. "
        "Format the whole answer as GitHub-flavored markdown."
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
