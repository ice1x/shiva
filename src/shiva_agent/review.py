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

# LLM providers the generated review workflow can target (task 00019). The MVP
# hardwired the review to Anthropic's Messages API — endpoint, request body, and
# even the response-parsing expression were Anthropic-specific — which vendor-
# locked the one step that actually produces the review. Lifting the provider
# into config removes that lock: a repo points the review at whatever model it
# already pays for, or a free local one. DeepSeek, Qwen, OpenAI and Ollama all
# speak the OpenAI `/chat/completions` schema, so they share a single "openai"
# request/response shape and differ only in endpoint + default model (and, for
# Ollama, needing no API key at all). Each preset:
#   api:           request/response shape — "anthropic" or "openai"
#   endpoint:      default HTTP endpoint (overridable, e.g. self-hosted Ollama)
#   auth_header:   HTTP Header Auth credential name, or None for keyless (Ollama)
#   default_model: model used when the config does not name one
DEFAULT_LLM_PROVIDER = "anthropic"
LLM_PROVIDERS = {
    "anthropic": {
        "api": "anthropic",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "auth_header": "x-api-key",
        "default_model": "claude-opus-4-8",
    },
    "openai": {
        "api": "openai",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "auth_header": "Authorization",  # value: "Bearer <API key>"
        "default_model": "gpt-4o",
    },
    "deepseek": {
        "api": "openai",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "auth_header": "Authorization",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "api": "openai",
        # Alibaba DashScope OpenAI-compatible mode (international endpoint).
        "endpoint": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
        "auth_header": "Authorization",
        "default_model": "qwen-plus",
    },
    "ollama": {
        "api": "openai",
        # Local Ollama's OpenAI-compatible endpoint; no API key required.
        "endpoint": "http://localhost:11434/v1/chat/completions",
        "auth_header": None,
        "default_model": "qwen2.5",
    },
}

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


def validate_config(config, partial=False, require_enabled=False):
    """Validate a review config, raising ConfigError with a clear message.

    Per-repo overrides (task 00014) invite users to hand-write a `.shiva.yml`, so
    a typo — an enabled category with no `prompt`, `enabled: "yes"` as a string, a
    duplicated `id` — must fail the build with an actionable message instead of a
    bare `KeyError` deep inside `load_enabled_categories` (task 00015).

    Checks: `config` is a mapping; `conventions` (if present) is a string;
    `exclude` (if present) is a list of non-empty string glob patterns;
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

    With `require_enabled=True` (only meaningful on a full, non-partial config)
    the config must enable at least one category (task 00016): otherwise the
    review prompt would carry an empty "Review categories" section — a reviewer
    with nothing to evaluate against. It is off by default so a structural check
    of an override, which legitimately disables categories, still passes.
    """
    if not isinstance(config, dict):
        raise ConfigError(f"config must be a mapping, got {type(config).__name__}")

    conventions = config.get("conventions")
    if conventions is not None and not isinstance(conventions, str):
        raise ConfigError(
            f"conventions must be a string, got {type(conventions).__name__}"
        )

    exclude = config.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list):
            raise ConfigError(f"exclude must be a list, got {type(exclude).__name__}")
        for i, pattern in enumerate(exclude):
            if not isinstance(pattern, str) or not pattern.strip():
                raise ConfigError(f"exclude[{i}] must be a non-empty string glob pattern")

    validate_llm(config.get("llm"))

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

    if require_enabled and not partial:
        if not any(cat.get("enabled") is True for cat in categories):
            raise ConfigError(
                "no review categories are enabled; enable at least one category "
                "with 'enabled: true' so the review has something to check"
            )


def validate_llm(llm):
    """Validate an `llm` config block, raising ConfigError with a clear message.

    The block is optional (absent → the Anthropic default). When present it must
    be a mapping; `provider` (if given) must be one of the supported providers
    (task 00019); and `model`/`endpoint` (if given) must be non-empty strings.
    Returns None on success.
    """
    if llm is None:
        return
    if not isinstance(llm, dict):
        raise ConfigError(f"llm must be a mapping, got {type(llm).__name__}")
    provider = llm.get("provider")
    if provider is not None and provider not in LLM_PROVIDERS:
        known = ", ".join(sorted(LLM_PROVIDERS))
        raise ConfigError(
            f"llm.provider {provider!r} is not supported; choose one of: {known}"
        )
    for field in ("model", "endpoint"):
        value = llm.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"llm.{field} must be a non-empty string")


def resolve_llm(config, override=None):
    """Return the effective LLM provider settings after an optional override.

    Removes the MVP's Anthropic vendor lock (task 00019): the review target is
    chosen from `LLM_PROVIDERS` by the config's `llm.provider` (default
    `anthropic`), with `llm.model` / `llm.endpoint` overriding the preset. A
    target repo's `.shiva.yml` `llm` block *replaces* the base block wholesale
    (the same override-wins rule as `conventions`/`exclude`), so switching
    provider in one repo never half-inherits the base provider's model.

    Returns a dict: ``{provider, api, endpoint, model, auth_header}`` — `api` is
    the request/response shape (``anthropic`` or ``openai``) and `auth_header`
    is the HTTP Header Auth credential name, or None for a keyless local
    provider (Ollama).
    """
    for src in (override, config):
        block = (src or {}).get("llm")
        if block:
            break
    else:
        block = {}
    validate_llm(block)
    name = block.get("provider", DEFAULT_LLM_PROVIDER)
    preset = LLM_PROVIDERS[name]
    return {
        "provider": name,
        "api": preset["api"],
        "endpoint": block.get("endpoint") or preset["endpoint"],
        "model": block.get("model") or preset["default_model"],
        "auth_header": preset["auth_header"],
    }


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
    KeyError. The effective config must also enable at least one category
    (task 00016), so an override that turns every category off fails the build
    rather than producing a reviewer with an empty prompt. With no override it is
    equivalent to `load_enabled_categories(config)` on a valid config.
    """
    if override is not None:
        validate_config(override, partial=True)
    merged = merge_config(config, override)
    validate_config(merged, require_enabled=True)
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


def resolve_exclude(config, override=None):
    """Return the file-exclusion glob patterns after applying an optional override.

    Generated, vendored, and lock files (`poetry.lock`, `*.min.js`, `dist/*`,
    …) carry a diff but are not worth a paid LLM review (task 00017), so
    `filter_files` drops any file whose path matches one of these globs. The
    defaults live in `shiva.config.yml`; a target repo's `.shiva.yml` `exclude`
    list *replaces* them wholesale (the same override-wins rule as
    `resolve_conventions`), so the repo keeps full control of what is reviewed.
    Returns a fresh list (never an alias of a config list); `[]` when neither
    provides a non-empty `exclude`.
    """
    for src in (override, config):
        exclude = (src or {}).get("exclude")
        if exclude:
            return list(exclude)
    return []


def filter_files(
    files, allowed_extensions=None, max_patch_chars=DEFAULT_MAX_PATCH_CHARS, exclude_globs=None
):
    """Filter GitHub /pulls/{n}/files items down to reviewable ones.

    Drops files without a patch (binary), removed files, files whose path
    matches an `exclude_globs` pattern (generated/vendored/lock files, task
    00017), oversized patches, and — when allowed_extensions is given — files
    with other extensions. Each glob is matched against both the full
    repository-relative path and the bare basename (fnmatch semantics), so
    `package-lock.json` excludes the file at any depth while `*/dist/*` targets
    a directory.
    """
    from fnmatch import fnmatch

    kept = []
    for f in files:
        patch = f.get("patch")
        if not patch:
            continue
        if f.get("status") == "removed":
            continue
        name = f.get("filename", "")
        if exclude_globs:
            base = name.rsplit("/", 1)[-1]
            if any(fnmatch(name, g) or fnmatch(base, g) for g in exclude_globs):
                continue
        if len(patch) > max_patch_chars:
            continue
        if allowed_extensions is not None:
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

    Returns a list of file-lists. An empty input yields a single empty batch;
    the generated Code node filters out the no-reviewable-files case before it
    batches (task 00018), so in practice this is only reached with ≥1 file.
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
