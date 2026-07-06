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

# ---------------------------------------------------------------------------
# Vendor-agnostic LLM layer (task 00020)
# ---------------------------------------------------------------------------
# The review step must not be tied to one vendor. A *provider* is nothing but an
# HTTP endpoint + a model name + an auth scheme, bound to one of a small set of
# wire protocols ("API families"). Two families cover essentially the whole
# market: Anthropic's Messages API, and the OpenAI `/chat/completions` API that
# OpenAI, DeepSeek, Qwen, OpenRouter, Ollama, vLLM and LM Studio all speak.
#
# Adding a brand-new wire protocol = one `LLMApi` subclass. Adding a vendor that
# speaks an existing protocol = one row in `LLM_PROVIDERS` — or nothing at all:
# a config block may define a provider inline (`api` + `endpoint` + `model`
# [+ `auth`]), so any OpenAI-/Anthropic-compatible server works without a code
# change. This layer is build-time only (never embedded in the n8n Code node),
# so it is free to use classes.
PROMPT_SENTINEL = "__SHIVA_PROMPT__"  # replaced with the n8n prompt expression at build time
MAX_OUTPUT_TOKENS = 16000
# Sampling temperature for the review. Default 0 → deterministic, terse, on-topic
# output (a review wants precision, not creativity). Overridable per repo via
# `llm.temperature`. Applied to the OpenAI-family body; Anthropic keeps adaptive
# thinking, which is incompatible with a fixed temperature.
DEFAULT_TEMPERATURE = 0


class LLMApi:
    """Interface for one LLM wire protocol — the vendor-agnostic seam.

    A concrete family describes, in provider-neutral terms, *how* to call the
    API and *where* the review text lives in the response. `build_workflow`
    turns that into the actual n8n nodes; nothing here knows about a vendor.
    """

    api = ""

    def request_body(self, model, temperature=DEFAULT_TEMPERATURE):
        """Return the JSON request body, with PROMPT_SENTINEL where the prompt goes."""
        raise NotImplementedError

    def comment_body_expr(self):
        """Return the n8n expression that extracts the review text from the response."""
        raise NotImplementedError

    def http_headers(self):
        """Return protocol-specific HTTP headers (auth is added separately)."""
        return [{"name": "content-type", "value": "application/json"}]

    def agent_chat_node(self, model, endpoint):
        """Return {type, typeVersion, parameters} for the agent variant's chat sub-node."""
        raise NotImplementedError


class AnthropicApi(LLMApi):
    """Anthropic Messages API — typed `content` blocks, adaptive thinking."""

    api = "anthropic"

    def request_body(self, model, temperature=DEFAULT_TEMPERATURE):
        # Anthropic's adaptive thinking is incompatible with a fixed temperature,
        # so `temperature` is intentionally not sent here.
        return {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": PROMPT_SENTINEL}],
        }

    def comment_body_expr(self):
        return (
            "={{ JSON.stringify({ body: $json.content"
            ".filter(b => b.type === 'text').map(b => b.text).join('\\n') }) }}"
        )

    def http_headers(self):
        return [
            {"name": "anthropic-version", "value": "2023-06-01"},
            {"name": "content-type", "value": "application/json"},
        ]

    def agent_chat_node(self, model, endpoint):
        return {
            "type": "@n8n/n8n-nodes-langchain.lmChatAnthropic",
            "typeVersion": 1.3,
            "parameters": {
                "model": {"__rl": True, "mode": "list", "value": model},
                "options": {"maxTokensToSample": MAX_OUTPUT_TOKENS},
            },
        }


class OpenAIApi(LLMApi):
    """OpenAI `/chat/completions` — spoken by OpenAI, DeepSeek, Qwen, OpenRouter,
    Ollama, vLLM, LM Studio and most self-hosted gateways."""

    api = "openai"

    def request_body(self, model, temperature=DEFAULT_TEMPERATURE):
        return {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": temperature,
            "messages": [{"role": "user", "content": PROMPT_SENTINEL}],
        }

    def comment_body_expr(self):
        return "={{ JSON.stringify({ body: $json.choices[0].message.content }) }}"

    def agent_chat_node(self, model, endpoint):
        base_url = endpoint.rsplit("/chat/completions", 1)[0]
        return {
            "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
            "typeVersion": 1.2,
            "parameters": {
                "model": {"__rl": True, "mode": "list", "value": model},
                "options": {"baseURL": base_url, "maxTokens": MAX_OUTPUT_TOKENS},
            },
        }


# Registry of API families. Add a subclass here to support a new wire protocol.
LLM_APIS = {api.api: api for api in (AnthropicApi(), OpenAIApi())}

# Auth schemes: how (or whether) the API key rides on the request. `header` is
# the HTTP Header Auth credential name; `value_hint` is what the operator puts
# in it. `none` is a keyless local server.
AUTH_SCHEMES = {
    "none": {"header": None, "value_hint": None},
    "bearer": {"header": "Authorization", "value_hint": "Bearer <API key>"},
    "x-api-key": {"header": "x-api-key", "value_hint": "<API key>"},
}

# Provider presets: endpoint + default model + auth for known vendors. Local,
# free, keyless providers come first, and the default is one of them ON PURPOSE
# so the out-of-the-box workflow costs nothing and locks in no vendor. The local
# endpoints use `host.docker.internal` because n8n runs in a container (see
# docker-compose.yml); a natively-run n8n should override `endpoint` to
# `localhost`. `model`/`endpoint`/`auth` are all overridable per repo.
LLM_PROVIDERS = {
    "ollama": {
        "api": "openai",
        "endpoint": "http://host.docker.internal:11434/v1/chat/completions",
        "default_model": "llama3.2",
        "auth": "none",
    },
    "lmstudio": {
        "api": "openai",
        "endpoint": "http://host.docker.internal:1234/v1/chat/completions",
        "default_model": "local-model",  # set llm.model to your loaded model id
        "auth": "none",
    },
    "vllm": {
        "api": "openai",
        "endpoint": "http://host.docker.internal:8000/v1/chat/completions",
        "default_model": "local-model",  # set llm.model to the served model id
        "auth": "none",
    },
    "openrouter": {
        "api": "openai",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "meta-llama/llama-3.1-8b-instruct:free",  # a free model
        "auth": "bearer",
    },
    "openai": {
        "api": "openai",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "auth": "bearer",
    },
    "deepseek": {
        "api": "openai",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "default_model": "deepseek-chat",
        "auth": "bearer",
    },
    "qwen": {
        "api": "openai",
        # Alibaba DashScope OpenAI-compatible mode (international endpoint).
        "endpoint": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
        "default_model": "qwen-plus",
        "auth": "bearer",
    },
    "anthropic": {
        "api": "anthropic",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-opus-4-8",
        "auth": "x-api-key",
    },
}
# Free, local, keyless — no vendor lock and no bill for a fresh clone.
DEFAULT_LLM_PROVIDER = "ollama"

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

    by_repo = config.get("llm_by_repo")
    if by_repo is not None:
        if not isinstance(by_repo, dict):
            raise ConfigError(
                f"llm_by_repo must be a mapping of 'owner/repo' → llm block, "
                f"got {type(by_repo).__name__}"
            )
        for repo, block in by_repo.items():
            if not isinstance(repo, str) or "/" not in repo:
                raise ConfigError(
                    f"llm_by_repo key {repo!r} must be an 'owner/repo' string"
                )
            try:
                validate_llm(block)
            except ConfigError as exc:
                raise ConfigError(f"llm_by_repo[{repo!r}]: {exc}") from exc

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
    """Validate an `llm` config block (task 00020), raising a clear ConfigError.

    The block is optional (absent → the default provider). It may either name a
    known `provider` (optionally overriding `model`/`endpoint`/`auth`), or define
    a provider inline for any compatible server via `api` + `endpoint`
    (+ `model`, and `auth` if the server needs a key). Rules:

    - the block is a mapping;
    - `api` (if given) is a known family and `auth` (if given) a known scheme;
    - `provider`/`model`/`endpoint` (if given) are non-empty strings;
    - a `provider` that is not a known preset is treated as an inline/custom
      provider and must supply `api` + `endpoint` + `model`, else it is rejected.

    Returns None on success.
    """
    if llm is None:
        return
    if not isinstance(llm, dict):
        raise ConfigError(f"llm must be a mapping, got {type(llm).__name__}")

    for field in ("provider", "model", "endpoint"):
        value = llm.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"llm.{field} must be a non-empty string")

    api = llm.get("api")
    if api is not None and api not in LLM_APIS:
        known = ", ".join(sorted(LLM_APIS))
        raise ConfigError(f"llm.api {api!r} is not supported; choose one of: {known}")

    auth = llm.get("auth")
    if auth is not None and auth not in AUTH_SCHEMES:
        known = ", ".join(sorted(AUTH_SCHEMES))
        raise ConfigError(f"llm.auth {auth!r} is not supported; choose one of: {known}")

    temperature = llm.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise ConfigError("llm.temperature must be a number between 0 and 2")

    provider = llm.get("provider")
    if provider is not None and provider not in LLM_PROVIDERS:
        # Unknown name is allowed only as an inline/custom provider, which must
        # fully specify how to reach it.
        missing = [k for k in ("api", "endpoint", "model") if not llm.get(k)]
        if missing:
            known = ", ".join(LLM_PROVIDERS)
            raise ConfigError(
                f"llm.provider {provider!r} is not a known provider ({known}); "
                f"to use a custom endpoint also set: {', '.join(missing)}"
            )


def resolve_llm(config, override=None, repo=None):
    """Resolve the effective LLM provider spec (task 00020).

    Vendor-agnostic: the winning `llm` block either names a known `provider` —
    with optional `model`/`endpoint`/`auth`/`temperature` overrides — or defines
    a provider inline via `api` + `endpoint` (+ `model`, + `auth`) for any
    compatible server. An empty or absent block yields `DEFAULT_LLM_PROVIDER`, a
    free, local, keyless provider, so a fresh clone costs nothing.

    Precedence for the winning block (each *replaces* the base wholesale, the
    same rule as `conventions`/`exclude`):

    1. an explicit ``override`` (a target repo's `.shiva.yml` `llm` block);
    2. a per-repo entry ``config["llm_by_repo"][repo]`` when ``repo`` is given —
       the central "default model + per-repo models that differ" mapping, so one
       config picks Ollama by default but e.g. OpenAI for `owner/repo`;
    3. the default ``config["llm"]``.

    Because n8n bakes the provider (and its credential) into the workflow at
    build time, this is resolved per built workflow — `build_workflow.py --repo
    owner/name` selects the mapped provider for that repo's workflow.

    Returns ``{provider, api, endpoint, model, auth, temperature}`` where `api`
    is the wire protocol (a key of `LLM_APIS`) and `auth` a key of `AUTH_SCHEMES`.
    """
    by_repo = (config or {}).get("llm_by_repo") or {}
    if (override or {}).get("llm"):
        block = override["llm"]
    elif repo is not None and repo in by_repo:
        block = by_repo[repo]
    else:
        block = (config or {}).get("llm") or {}
    validate_llm(block)

    provider = block.get("provider")
    inline = provider is None and any(k in block for k in ("api", "endpoint"))
    if provider in LLM_PROVIDERS or (provider is None and not inline):
        # A known provider preset (or the default when nothing is specified),
        # with optional field overrides.
        name = provider or DEFAULT_LLM_PROVIDER
        preset = LLM_PROVIDERS[name]
        api = block.get("api") or preset["api"]
        endpoint = block.get("endpoint") or preset["endpoint"]
        model = block.get("model") or preset["default_model"]
        auth = block.get("auth") or preset["auth"]
    else:
        # Inline/custom provider — no preset to fall back on, so it must be
        # fully specified.
        name = provider or "custom"
        api = block.get("api")
        endpoint = block.get("endpoint")
        model = block.get("model")
        auth = block.get("auth") or "none"
        missing = [k for k, v in (("api", api), ("endpoint", endpoint), ("model", model)) if not v]
        if missing:
            raise ConfigError(
                f"custom llm provider {name!r} must set: {', '.join(missing)}"
            )
    temperature = block.get("temperature", DEFAULT_TEMPERATURE)
    return {
        "provider": name,
        "api": api,
        "endpoint": endpoint,
        "model": model,
        "auth": auth,
        "temperature": temperature,
    }


def llm_auth(auth):
    """Return ``(header_name_or_None, value_hint_or_None)`` for an auth scheme."""
    scheme = AUTH_SCHEMES[auth]
    return scheme["header"], scheme["value_hint"]


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


def match_glob(name, pattern):
    """fnmatch-style wildcard match implemented without any stdlib import.

    Supports the glob syntax the exclude patterns use: ``*`` (any run, including
    ``/``), ``?`` (one char), and ``[seq]`` / ``[!seq]`` character classes.
    Case-sensitive and anchored to the whole string, matching ``fnmatch``
    semantics on POSIX — which is what the n8n Linux Python runner uses.

    Hand-rolled because that runner's sandbox disallows *every* stdlib import
    ("Allowed stdlib modules: none"), so neither ``fnmatch`` nor ``re`` can be
    imported inside the generated Code node (task 00017). This function's source
    is embedded verbatim into the node by scripts/build_workflow.py.
    """
    # Tokenize the pattern once: ("star",) | ("any",) | ("lit", ch)
    # | ("set", negate, items) where items are chars or (lo, hi) ranges.
    tokens = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            tokens.append(("star",))
            i += 1
        elif c == "?":
            tokens.append(("any",))
            i += 1
        elif c == "[":
            j = i + 1
            negate = False
            if j < n and pattern[j] in "!^":
                negate = True
                j += 1
            items, first = [], True
            while j < n and (pattern[j] != "]" or first):
                first = False
                if j + 2 < n and pattern[j + 1] == "-" and pattern[j + 2] != "]":
                    items.append((pattern[j], pattern[j + 2]))
                    j += 3
                else:
                    items.append(pattern[j])
                    j += 1
            if j >= n:  # unterminated "[" — treat it as a literal
                tokens.append(("lit", "["))
                i += 1
                continue
            tokens.append(("set", negate, items))
            i = j + 1
        else:
            tokens.append(("lit", c))
            i += 1

    def _matches(ch, token):
        kind = token[0]
        if kind == "any":
            return True
        if kind == "lit":
            return ch == token[1]
        _, negate, items = token  # ("set", negate, items)
        hit = False
        for it in items:
            if isinstance(it, tuple):
                if it[0] <= ch <= it[1]:
                    hit = True
                    break
            elif ch == it:
                hit = True
                break
        return hit != negate

    # Greedy wildcard match with backtracking on the last "*".
    ti = ni = 0
    star_ti, star_ni = -1, 0
    nt, nn = len(tokens), len(name)
    while ni < nn:
        if ti < nt and tokens[ti][0] == "star":
            star_ti, star_ni = ti, ni
            ti += 1
        elif ti < nt and tokens[ti][0] != "star" and _matches(name[ni], tokens[ti]):
            ti += 1
            ni += 1
        elif star_ti != -1:
            ti = star_ti + 1
            star_ni += 1
            ni = star_ni
        else:
            return False
    while ti < nt and tokens[ti][0] == "star":
        ti += 1
    return ti == nt


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
            if any(match_glob(name, g) or match_glob(base, g) for g in exclude_globs):
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
        "Be terse and specific: every finding one line, concrete, no preamble, no "
        "restating the diff, no filler. Say only what matters.",
        "",
    ]
    # Review discipline: accuracy + no-nitpick rules to curb the failure modes
    # seen on real reviews (hallucinated line numbers, misread code, style/rename
    # nitpicks, suggestions that fight intentional design). See data/README.md.
    lines.append("# Review discipline")
    lines.append(
        "- Flag ONLY what you can tie to specific code shown in the diff; quote or "
        "name the exact symbol. Never infer or assume code you cannot see."
    )
    lines.append(
        "- Cite exact line numbers from the diff. If you are not certain of the line, "
        "omit the number rather than guess."
    )
    lines.append(
        "- Do NOT raise renames, file/module splits, or micro-optimizations unless "
        "they fix a concrete correctness or clarity bug. Style nits are out of scope "
        "unless a Code Style category is enabled above."
    )
    lines.append(
        "- A wrong or speculative finding is worse than a missed nit: prefer fewer, "
        "verifiable findings, and treat the repository conventions below as intentional."
    )
    lines.append("")
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
