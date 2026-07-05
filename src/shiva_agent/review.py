"""Diff filtering and review-prompt assembly for the PR review agent.

The bodies of `filter_files` and `build_review_prompt` are embedded verbatim
into the n8n Code node by scripts/build_workflow.py, so they must stay
dependency-free (standard library only) and self-contained.
"""

DEFAULT_MAX_PATCH_CHARS = 15_000


def load_enabled_categories(config):
    """Return [{'name', 'prompt'}, ...] for categories with enabled: true."""
    return [
        {"name": c["name"], "prompt": c["prompt"].strip()}
        for c in config.get("categories", [])
        if c.get("enabled")
    ]


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
