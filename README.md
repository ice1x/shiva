# Shiva — a configurable LLM code reviewer for pull requests

[![CI](https://github.com/ice1x/shiva/actions/workflows/ci.yml/badge.svg)](https://github.com/ice1x/shiva/actions/workflows/ci.yml)

Shiva reviews a pull request against **categories you choose**, under **house
rules the repository states**, with **whatever model that repository already
pays for** (or a free local one), and posts the findings as one PR comment.

It runs as a GitHub Action — no server, no tunnel, no always-on machine. The
original n8n runtime still works and is documented in [docs/n8n.md](docs/n8n.md).

## Quickstart

Add `SHIVA_LLM_API_KEY` to the repository's Actions secrets, then commit:

```yaml
# .github/workflows/shiva-review.yml
name: Shiva Review
on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ice1x/shiva@main
        with:
          llm-api-key: ${{ secrets.SHIVA_LLM_API_KEY }}
```

The next pull request gets reviewed. Full options, cost notes, and a
post-nothing dry run: [docs/github-action.md](docs/github-action.md).

```bash
# read a review before trusting it on a live PR — fetches and reviews for real,
# prints the comment instead of posting it
GITHUB_TOKEN=$(gh auth token) SHIVA_LLM_API_KEY=sk-... \
    python scripts/review_pr.py --repo owner/name --pr 42 --dry-run
```

## What a review looks for

Findings are terse, severity-tagged, and anchored to the diff:

```
1. **Summary** — one sentence on what the PR changes.
2. **Verdict** — comment | request changes
3. **Findings**
   - [high] Logical Review — src/app.py:214 — `parse()` returns None on a blank
     line and the caller dereferences it; guard before the `.strip()`.
```

A PR with nothing worth saying gets **no comment at all** — the model answers
with a single sentinel word and the run stops there, so a clean PR is not
decorated with filler.

## Review categories

The agent reviews code against a set of **configurable categories** defined
in [`shiva.config.yml`](shiva.config.yml). Each category has an `id`, a
`name`, an `enabled` flag, and a `prompt` block; the review prompt sent to
the LLM is assembled from the **enabled** categories only.

| # | Category | Default | What it covers |
|---|----------|---------|----------------|
| 1 | **Structural** | ✅ on | Architecture and design of the code: logical organization, best practices for structure |
| 2 | **Logical** | ✅ on | Logic and algorithms: correctness and efficiency of the implemented logic |
| 3 | **Behavioral** | ✅ on | Behavior in different scenarios: functional requirements are met, edge cases handled properly |
| 4 | **Security** | ✅ on | Potential security vulnerabilities: best practices for security and data protection |
| 5 | **Performance** | ✅ on | Performance issues: code optimized for speed and resource usage |
| 6 | **Code Style** | ⬜ off | Adherence to the project's coding standards and style guidelines: naming conventions, indentation, formatting consistency |
| 7 | **Docstrings & Comments** | ⬜ off | In-code documentation: docstrings present and accurate, comments clear and helpful |
| 8 | **Messages** | ⬜ off | Clarity and helpfulness of error messages and log outputs: informative, aid debugging and understanding |
| 9 | **Test Coverage** | ⬜ off | Tests associated with the code: adequate coverage of various scenarios and edge cases |

**Custom categories are supported**: add a new entry to the `categories`
list with your own `id`, `name`, and `prompt` (see the commented example in
[`shiva.config.yml`](shiva.config.yml)) — it is treated exactly like the
built-in ones.

`shiva.config.yml` in this repo is the default configuration and reference
schema. A target repository overrides it with its own `.shiva.yml`, merged over
the defaults by category `id` — see
[Per-repo configuration](#per-repo-configuration).

### Per-repo configuration

A target repo tailors the review by shipping a `.shiva.yml` that is merged over
[`shiva.config.yml`](shiva.config.yml) **by category `id`**:

- an entry whose `id` matches a default overrides only the fields it lists —
  `{id: performance, enabled: false}` just turns that category off and keeps its
  name and prompt;
- an entry with a new `id` is added as a first-class custom category;
- defaults left unmentioned are untouched, and their order is preserved.

A top-level `conventions:` block (free-form house rules — coding standards,
context, no-go rules) is likewise overridden by the target repo's value and
injected into the review prompt, so the review respects the
project's own conventions:

```yaml
# target-repo/.shiva.yml
conventions: |
  Prefer pure functions; keep side effects at the edges.
  Never log secrets or PII. Public APIs need docstrings.
categories:
  - id: performance
    enabled: false
```

The merge is implemented and unit-tested in
[`merge_config`](src/shiva_agent/review.py). A hand-written `.shiva.yml` is
validated before it is used: the merged, effective config is
checked by [`validate_config`](src/shiva_agent/review.py), so a typo — a
category with no `prompt`, a duplicated `id`, `enabled: "yes"` as a string, or
`categories:` written as a mapping — fails the build with a clear message naming
the offending category (`error: invalid config in .shiva.yml: category 'x' is
missing a non-empty string 'prompt'`) instead of a bare `KeyError`. The
effective config must also enable **at least one** category: an override that
turns every category off is rejected (`error: invalid config in .shiva.yml: no
review categories are enabled ...`) rather than silently producing a reviewer
whose prompt has an empty "Review categories" section and nothing to check.

Running as a GitHub Action, the override is read **from the checkout on every
run**, so a repo changes its review policy by editing `.shiva.yml` — nothing to
rebuild. (Under the n8n runtime the config is baked in at build time instead;
see [docs/n8n.md](docs/n8n.md).)

## Excluded files

Some changed files carry a diff but are not worth a paid LLM review — lock files
(`poetry.lock`, `package-lock.json`, …), source maps, minified bundles, and
vendored or generated code. `filter_files` drops any file whose path matches one
of the `exclude` glob patterns in [`shiva.config.yml`](shiva.config.yml)
*before* the diff is batched and sent to the model, so those files
never cost a review call and never clutter the findings.

Each glob is matched (fnmatch semantics) against **both** the full
repository-relative path and the bare basename, so `package-lock.json` excludes
the file at any depth while `*/dist/*` targets a directory. The shipped defaults
cover the usual suspects:

```yaml
# shiva.config.yml
exclude:
  - "*.lock"          # poetry.lock, Cargo.lock, yarn.lock, Gemfile.lock, ...
  - "package-lock.json"
  - "*.min.js"
  - "*.map"
  - "*/node_modules/*"
  - "*/dist/*"
  # ... see the file for the full list
```

A target repo's `.shiva.yml` `exclude` list **replaces** these defaults wholesale
(the same override-wins rule as `conventions`), so copy the ones you still want
and add your own. The list is validated with the rest of the config:
a non-list `exclude`, or an empty/blank pattern, is rejected with a clear
message.

If **every** changed file is filtered out — a PR that only bumps `poetry.lock`,
touches binaries, or deletes files — the run stops before the model call.
Nothing is reviewed, nothing is paid for, and no "no reviewable files" comment
is posted. The same holds for a draft PR or one labelled `skip-review`: the gate
runs before any request.

## Choosing the LLM provider

The review step is **vendor-agnostic**: a small
provider-neutral layer ([`LLMApi`](src/shiva_agent/review.py)) speaks two wire
protocols — Anthropic's Messages API and the OpenAI `/chat/completions` API that
everything else uses. The `llm` block in
[`shiva.config.yml`](shiva.config.yml) picks the provider; a target repo's
`.shiva.yml` `llm` block **replaces** it wholesale (the same override-wins rule as
`conventions`/`exclude`).

**The default is a free, local, keyless model — not a paid vendor.** A fresh
clone reviews with a local [Ollama](https://ollama.com) and costs nothing.

| `provider` | Kind | Default endpoint | Default model | Key |
|---|---|---|---|---|
| `ollama` **(default)** | local, free | `host.docker.internal:11434/v1/chat/completions` | `llama3.2` | none |
| `lmstudio` | local, free | `host.docker.internal:1234/...` | *(set `model`)* | none |
| `vllm` | local/self-hosted | `host.docker.internal:8000/...` | *(set `model`)* | none |
| `openrouter` | hosted (free & paid) | `openrouter.ai/api/v1/chat/completions` | `…llama-3.1-8b-instruct:free` | Bearer |
| `openai` | hosted | `api.openai.com/v1/chat/completions` | `gpt-4o-mini` | Bearer |
| `deepseek` | hosted | `api.deepseek.com/v1/chat/completions` | `deepseek-chat` | Bearer |
| `qwen` | hosted | DashScope compatible-mode | `qwen-plus` | Bearer |
| `anthropic` | hosted | `api.anthropic.com/v1/messages` | `claude-opus-4-8` | `x-api-key` |

All providers except `anthropic` share the OpenAI `/chat/completions` shape (the
comment body is read from `choices[0].message.content`); Anthropic uses its
Messages API (`content[].text`, adaptive thinking). `model` and `endpoint` are
optional overrides (e.g. run a bigger local model, or point `ollama` at a remote
GPU box):

```yaml
# target-repo/.shiva.yml
llm:
  provider: ollama          # ollama | lmstudio | vllm | openrouter | openai | deepseek | qwen | anthropic
  model: qwen2.5-coder      # optional; overrides the provider default
```

**Any compatible endpoint, no preset needed** — the real vendor-agnostic escape
hatch. Define a provider inline with `api` + `endpoint` (+ `model`, + `auth`), so
a self-hosted gateway or an unlisted vendor works without a code change:

```yaml
# target-repo/.shiva.yml
llm:
  api: openai               # wire protocol: openai | anthropic
  endpoint: https://llm.mycorp.internal/v1/chat/completions
  model: my-model
  auth: bearer              # none | bearer | x-api-key
```

## Keeping reviews trustworthy

An LLM reviewer earns trust slowly and loses it in one bad comment, so the
output is constrained on three sides:

- **Prompt discipline.** The prompt forbids findings that cannot be tied to code
  shown in the diff, forbids claiming something is missing when it may live in
  code not shown, and rules out renames, micro-optimizations, and style nits
  unless a style category is enabled. A wrong finding is treated as worse than a
  missed one.
- **Post-processing.** The answer is sanitized before it is posted: a code fence
  wrapping the whole review is unwrapped (GitHub would otherwise render the
  review as one grey block), and a line number that does not exist in the diff is
  stripped while the path is kept — an invented `file.ts:0` anchor is exactly
  what makes a reader stop believing the rest.
- **A feedback loop.** Verdicts on real findings are logged and turned into
  sharper prompt rules and per-repo conventions — see
  [docs/improving-reviews.md](docs/improving-reviews.md).

Known limits, honestly: findings are only as good as the model behind them, a
diff-only review cannot see the code it was not shown, and the reviewer is
better at local correctness than at architecture.

## Large pull requests

A big PR is not squeezed into one oversized prompt. After filtering, files are
packed greedily into size-bounded passes (`DEFAULT_MAX_BATCH_CHARS`, 45k; a
single file over the budget still gets its own pass rather than being dropped),
and each prompt states which part it is so the model scopes findings to the
files it was shown instead of flagging the split-off ones as missing. One model
call and one comment per pass.

## How it is built

```
src/shiva_agent/review.py    review policy: config merge, filtering, batching,
                             prompt assembly, output sanitizing (pure, no imports —
                             parts of it are embedded into an n8n Python sandbox)
src/shiva_agent/action.py    the runtime: gate → fetch → review → post, with HTTP
                             injected, so the whole path is unit-tested offline
scripts/review_pr.py         the I/O shell (urllib) the Action runs
scripts/build_workflow.py    generates the n8n workflow JSON from the same logic
```

Decisions live in pure modules, I/O lives in `scripts/`. `pytest` runs the whole
suite with no network and no secrets.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Documentation

| Document | What is in it |
|---|---|
| [docs/github-action.md](docs/github-action.md) | Running it on GitHub Actions: inputs, providers, cost, dry runs |
| [docs/n8n.md](docs/n8n.md) | The original n8n runtime: local setup, tunnel, scripted end-to-end run |
| [docs/improving-reviews.md](docs/improving-reviews.md) | Logging verdicts on findings and turning them into prompt rules |
| [docs/migration-off-n8n.md](docs/migration-off-n8n.md) | Why the runtime moved, and what is left of n8n |
| [docs/roadmap.md](docs/roadmap.md) | Delivery log and what is next |

## Status

The MVP is done and the GitHub Action runtime replaces the laptop-and-tunnel
setup it started as. Reviews have run against real pull requests in
`ice1x/graphbook` and `ice1x/antigram`. See [docs/roadmap.md](docs/roadmap.md)
for the delivery log.

## License

MIT — see [LICENSE](LICENSE).
