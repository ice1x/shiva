# n8n PR Review Agent (MVP)

[![CI](https://github.com/ice1x/shiva/actions/workflows/ci.yml/badge.svg)](https://github.com/ice1x/shiva/actions/workflows/ci.yml)

## Goal

Build a minimal, working code-review agent in n8n as fast as possible and start using it on local pet projects. When a Pull Request is opened, n8n fetches the diff, sends it to an LLM with a review prompt, and posts the result back as a PR comment.

**Priority: speed over polish.** Get the happy path working end-to-end first, then iterate.

## Scope (MVP)

- Trigger on `pull_request opened` events from GitHub
- Fetch the PR diff via GitHub API
- Filter/prepare the diff in a Code node (Python-friendly)
- Send to an LLM with a "review this code" prompt
- Post the review as a comment on the PR

Out of scope for MVP: multi-file looping, agentic tool use, draft/label filtering, CI integration.

## Review Categories

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
the defaults by category `id` (task `00014`) — see
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
injected into the review prompt (task `00012`), so the review respects the
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
validated before it is used (task `00015`): the merged, effective config is
checked by [`validate_config`](src/shiva_agent/review.py), so a typo — a
category with no `prompt`, a duplicated `id`, `enabled: "yes"` as a string, or
`categories:` written as a mapping — fails the build with a clear message naming
the offending category (`error: invalid config in .shiva.yml: category 'x' is
missing a non-empty string 'prompt'`) instead of a bare `KeyError`. The
effective config must also enable **at least one** category (task `00016`): an
override that turns every category off fails the build (`error: invalid config
in .shiva.yml: no review categories are enabled; enable at least one category
with 'enabled: true' ...`) rather than silently generating a reviewer whose
prompt has an empty "Review categories" section and nothing to check. Because
review categories are resolved and embedded into the Code node at build time, a
per-repo workflow is produced by pointing the generator at the override file:

```bash
.venv/bin/python scripts/build_workflow.py \
    --override path/to/target-repo/.shiva.yml \
    -o workflows/pr_review.<repo>.json
```

Auto-fetching `.shiva.yml` from the target repo at run time (one shared workflow
for all repos) is a future step: it needs YAML parsing inside the n8n Python
Code node, which the stock native runner does not provide.

## Excluded files

Some changed files carry a diff but are not worth a paid LLM review — lock files
(`poetry.lock`, `package-lock.json`, …), source maps, minified bundles, and
vendored or generated code. `filter_files` drops any file whose path matches one
of the `exclude` glob patterns in [`shiva.config.yml`](shiva.config.yml)
(task `00017`) *before* the diff is batched and sent to the model, so those files
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
and add your own. The list is validated with the rest of the config
(task `00015`): a non-list `exclude`, or an empty/blank pattern, fails the build
with a clear message.

If **every** changed file is filtered out — a PR that only bumps `poetry.lock`,
touches binaries, or deletes files — the Code node emits **no** items at all
(task `00018`), so n8n skips the Claude call and the comment entirely. Nothing is
reviewed, nothing is paid for, and no "no reviewable files" comment is posted;
this mirrors the empty false-branch of the [skip gate](#task-list) (task `00010`)
and completes `00017`'s promise of never spending a review call on machine-generated
noise.

## Run n8n locally

```bash
# optional: cp .env.example .env and adjust port/timezone/webhook URL
docker compose up -d
open http://localhost:5678   # editor UI (first load shows owner setup)
```

Data (workflows, credentials, SQLite DB) persists in the `n8n_data` Docker
volume across restarts. To receive real GitHub webhooks later (task `00003`),
set `WEBHOOK_URL` in `.env` to a tunnel URL (ngrok/cloudflared) that forwards
to `localhost:5678`.

### Expose n8n for webhooks (dev tunnel)

GitHub can only deliver a webhook to a **public** URL, but n8n runs on
localhost. [`scripts/dev_tunnel.sh`](scripts/dev_tunnel.sh) opens a keyless
[Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
and wires it up in one step:

```bash
brew install cloudflared      # one-time
./scripts/dev_tunnel.sh
```

It waits for the tunnel's `https://<random>.trycloudflare.com` URL, writes it
into `.env` as `WEBHOOK_URL`, restarts n8n so it picks the URL up (n8n reads
`WEBHOOK_URL` only at startup), and prints the exact **Payload URL** to paste
into each repo's `Settings → Webhooks` — the same URL for every repo, since the
workflow reads `owner/repo` from the webhook body. The tunnel runs in the
foreground; **Ctrl-C tears the public URL down**, so nothing external can reach
n8n once you stop testing.

This is a manual, operator-run helper on purpose. It is **not** part of CI or
the test suite: a quick-tunnel URL is random and single-use (nothing
deterministic to assert), the end-to-end run (task `00008`) needs live GitHub
credentials and a real PR, and exposing a CI runner publicly would be a hole.
The free `trycloudflare` URL also changes on every restart, so re-run the script
(and re-paste the Payload URL) if you bring the tunnel back up.

**Security.** Your GitHub token never travels through the tunnel — only inbound
webhook payloads do; n8n's calls to `api.github.com` go out directly. The real
exposure is the n8n editor being publicly reachable. Mitigations, in order:

- **Create the n8n owner account first.** On n8n 2.x the editor is protected by
  built-in user management (the legacy `N8N_BASIC_AUTH_*` vars were removed), but
  until you finish the owner-setup wizard the instance is *unclaimed* — anyone
  who reaches the URL can claim it. The script **refuses to open a tunnel** while
  the owner account is missing, so complete setup at `http://localhost:5678`
  (strong password) first.
- **Keep the URL private** — the random subdomain is unguessable, but that is
  security-by-obscurity; don't paste or share it beyond the repo webhook config.
- **Stop the tunnel when done** — Ctrl-C tears the public URL down.

Compose starts two containers: `shiva-n8n` (the editor/engine) and
`shiva-n8n-runners` (the task-runner sidecar that executes Code-node
scripts). The sidecar is required: the stock n8n image ships no Python, so
without it every Python Code node fails with "Python runner unavailable".
Both images must be on the same version.

## Automated end-to-end run

The end-to-end path (task `00008`) is scripted by
[`scripts/e2e.py`](scripts/e2e.py) so it is not a manual click-through. Its pure
logic (payload/credential/webhook builders, review-comment detection) lives in
[`src/shiva_agent/e2e.py`](src/shiva_agent/e2e.py) and is unit-tested
([`tests/test_e2e.py`](tests/test_e2e.py)); the script itself orchestrates the
`n8n` CLI and `gh`.

**No secret is ever stored in the repo.** The GitHub token is resolved at
runtime from `$SHIVA_GITHUB_TOKEN` or `gh auth token`, written only to a `0600`
temp file that is deleted afterwards (and scrubbed from the container), and n8n
encrypts it into its own DB. The committed workflow JSON is never modified — a
credential-wired copy is imported from a temp file.

```bash
# local, deterministic, no external mutation — safe for CI:
.venv/bin/python scripts/e2e.py smoke     # POST a synthetic draft PR event,
                                          # assert the webhook + skip gate work

# import the PAT credential, wire + activate the workflow (idempotent):
.venv/bin/python scripts/e2e.py setup

# full live run against a real repo (needs a tunnel + owner account):
.venv/bin/python scripts/e2e.py live \
    --repo ice1x/graphbook \
    --base-url https://<your-tunnel>.trycloudflare.com
```

Every subcommand takes **`--dry-run`**: it prints exactly what it would do — the
webhook payload, the credential shape (**token redacted**), the n8n CLI commands,
and the ordered `live` plan with the flawed sample — while making **no** network
call, no n8n import, and no GitHub mutation. Preview a live run safely first:

```bash
.venv/bin/python scripts/e2e.py live --repo ice1x/graphbook \
    --base-url https://<your-tunnel>.trycloudflare.com --dry-run
```

`live` imports the credential, registers the repo webhook (idempotent), opens a
throwaway PR carrying intentionally flawed code, polls until the review comment
appears (or times out), prints PASS/FAIL, then closes the PR and deletes the
branch (`--keep` to leave them). It exits non-zero on failure, so it doubles as
a check you can wire into a manual pipeline — though **not** unattended CI: it
needs a live tunnel, real credentials, and a real PR.

> The pure logic is unit-tested, but the `n8n`-CLI / `gh` orchestration in
> `live`/`setup` is best-effort against n8n `2.28.6` and is confirmed on your
> first real run — the same caveat as the rest of task `00008`.

### Hosted-LLM keys (per-repo mapping)

When a repo's resolved provider is a hosted one (via `llm` or the `llm_by_repo`
mapping — see [Choosing the LLM provider](#choosing-the-llm-provider)), the
`LLM Review` node needs an API key, which the driver imports as a second n8n
credential from the environment (or gitignored `.env`). Because a mapping can
target several providers with different keys, the key is looked up **by
provider**, most specific first. Every name is `SHIVA_`-namespaced so it can't
accidentally pick up an unrelated key from the shell:

1. `SHIVA_<PROVIDER>_API_KEY` — e.g. `SHIVA_OPENAI_API_KEY`
2. `SHIVA_LLM_API_KEY` — generic fallback for the single-provider case

So a `.env` can hold every provider's key at once and each per-repo workflow
picks the right one:

```
SHIVA_OPENAI_API_KEY=sk-...
SHIVA_DEEPSEEK_API_KEY=...
```

Since n8n bakes the provider (and its credential) into a workflow at build time,
run `build_workflow.py --repo owner/name` per repo and deploy each separately;
one deployed workflow reviews with one provider. The default local Ollama needs
no key.

**Config-validation gate.** Before any pipeline action, `live`/`setup` validate
the config's structure and then the readiness of **every** mapped provider —
hosted *and* local — failing during validation rather than half-way through a
run. Readiness means: a hosted provider has its API key; a **local** provider
has a reachable server (checked from inside the n8n container, since that is who
calls it — the exact failure mode of a stopped Ollama). Check the whole mapping
up front without touching anything:

```bash
.venv/bin/python scripts/e2e.py check
#   [  ok] ollama     (local)  used by <default>        — reachable at http://host.docker.internal:11434/...
#   [FAIL] openai     (hosted) used by ice1x/graphbook  — no key — set SHIVA_OPENAI_API_KEY in .env
# check: FAIL — see above.
```

## Operating the pipeline (on/off, logs)

The pipeline is **live** when all of these are up at once; if any drops (you
close the tunnel terminal, reboot, stop n8n) reviews stop until it is back:

1. the n8n containers — `docker compose up -d` (they carry `restart:
   unless-stopped`, so they survive a reboot once Docker starts)
2. the model server — a local Ollama for keyless repos; nothing extra for a
   hosted provider
3. the public tunnel — `./scripts/dev_tunnel.sh`, kept running
4. the workflow **activated** and each repo's webhook registered — `scripts/e2e.py
   setup` (and `live` registers the webhook)

### On / off

Fastest is the **Active toggle in the n8n UI**: open `http://localhost:5678`,
open the **Shiva PR Review Agent** workflow, flip **Active** (top-right). Or from
the CLI (restart after, or the running process keeps serving the old state):

```bash
docker exec shiva-n8n n8n update:workflow --id=ShivaPrReview001 --active=false
docker restart shiva-n8n     # re-register the production webhook after a change
```

Other switches: **Ctrl-C the tunnel** (GitHub can no longer reach n8n — off, but
the engine stays up), toggle/delete a repo's hook under **Settings → Webhooks**
(disable one repo), or `docker compose stop` to stop everything.

### Logs / monitoring

- **n8n → Executions** (`http://localhost:5678` → *Executions*) — the primary
  log: one entry per run, every node, and the exact error where a run failed.
- **Container logs** — `docker compose logs -f n8n` (engine, webhook
  registration) and `docker logs shiva-n8n-runners` (the Python Code node).
- **GitHub → repo → Settings → Webhooks → Recent Deliveries** — what GitHub sent
  and the response code; the first place to look when *no* review appears (404 =
  workflow inactive / not re-registered, 200 = it reached n8n).
- The tunnel's terminal shows request logs.

**No review appeared?** Check Recent Deliveries first: a 404 means the workflow
isn't serving (run `setup`, or restart n8n); a 200 means it reached n8n, so open
the red run in **Executions** to see which node failed (401 = bad/missing API
key, connection refused = the model server is down).

## Improving the reviewer (leaving feedback)

Reviews are only as good as the prompt and the per-repo conventions behind them.
When a finding is wrong or a nitpick, record your verdict so it can be turned
into a rule — an append-only log at
[`data/review_feedback.jsonl`](data/review_feedback.jsonl) (schema + how it feeds
review improvement in [`data/README.md`](data/README.md)):

```bash
# log a verdict on one finding
.venv/bin/python scripts/feedback.py add \
    --repo ice1x/graphbook --pr 90 \
    --file src/bookmarks.ts --lines 58 --severity medium --category structural \
    --claim "isBookmark misleading; rename to isValidBookmark" \
    --verdict reject --root-cause misread_code \
    --reason "positive presence check, not validation"

# see what the bot systematically gets wrong (verdicts / root causes / categories)
.venv/bin/python scripts/feedback.py summary
```

Act on the signal in two places: recurring global mistakes → the "Review
discipline" rules in [`build_review_prompt`](src/shiva_agent/review.py);
repo-specific facts → that repo's `.shiva.yml` `conventions`
(see [Per-repo configuration](#per-repo-configuration)). A verify pass and
knowledge-graph retrieval are the later, larger steps described in
[`data/README.md`](data/README.md).

## Workflow as Code

The n8n workflow is generated, not hand-built. Sources of truth:

- [`shiva.config.yml`](shiva.config.yml) — review categories (enabled ones end up in the prompt), per-repo `conventions`, the [`exclude`](#excluded-files) globs for generated files, and the [`llm`](#choosing-the-llm-provider) provider
- [`src/shiva_agent/review.py`](src/shiva_agent/review.py) — diff filtering and prompt assembly (unit-tested, embedded verbatim into the Code node)
- [`scripts/build_workflow.py`](scripts/build_workflow.py) — assembles [`workflows/pr_review.json`](workflows/pr_review.json)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                              # run unit tests
.venv/bin/python scripts/build_workflow.py    # regenerate workflows/pr_review.json
# import into the running n8n container:
docker cp workflows/pr_review.json shiva-n8n:/tmp/pr_review.json
docker exec shiva-n8n n8n import:workflow --input=/tmp/pr_review.json
```

The imported workflow (`Shiva PR Review Agent`) wires: GitHub PR Webhook →
Check Skip Conditions (Python Code node) → Skip Draft & Labeled PRs? (IF) →
Fetch PR Files → Filter Diff & Build Prompt (Python Code node) → LLM Review
(the configured [provider](#choosing-the-llm-provider)) → Post PR Comment. Draft
PRs and PRs labeled `skip-review` end at the IF gate without any GitHub or LLM
calls (task `00010`). Before it can run
end-to-end you still need to attach credentials in the n8n UI (task `00002`):
an HTTP Header Auth credential `Authorization: Bearer <GitHub PAT>` on the two
GitHub nodes, plus a tunnel `WEBHOOK_URL` for real webhooks (task `00003`). The
`LLM Review` node needs a key only if the [chosen provider](#choosing-the-llm-provider)
is a hosted one — the **default is a free, local, keyless** model, so no LLM
credential is required to start.

### Choosing the LLM provider

The review step is **vendor-agnostic** (tasks `00019`/`00020`): a small
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

Because the provider is resolved and baked into the `LLM Review` node at build
time, generate a repo-specific workflow by pointing the generator at the override:

```bash
.venv/bin/python scripts/build_workflow.py \
    --override path/to/target-repo/.shiva.yml \
    -o workflows/pr_review.<repo>.json
```

The local endpoints use `host.docker.internal` so the dockerised n8n reaches a
model server on the host (docker-compose adds the `host-gateway` mapping); a
natively-run n8n should override `endpoint` to `localhost`. The **agent variant**
follows the provider too — OpenAI-compatible providers use the LangChain
`lmChatOpenAi` node with the provider's base URL instead of `lmChatAnthropic`. As
with the rest of the agent variant, its n8n node schemas are best-effort and
confirmed only under E2E (task `00008`).

### Large PRs

A big pull request is not squeezed into one oversized prompt (task `00011`).
After filtering, `Filter Diff & Build Prompt` packs the kept files into
size-bounded batches with `split_files_into_batches` — greedily, in order,
keeping each batch's combined patch length within `DEFAULT_MAX_BATCH_CHARS`
(45k; a single file that alone exceeds the budget still gets its own pass and
is never dropped). The Code node then returns **one item per batch**, so n8n
fans out natively: one `LLM Review` call and one `Post PR Comment` per
batch. Each prompt is told which part it is (`review part i of N`) so the
model scopes findings to the files shown instead of flagging the split-off
files as missing, and every emitted item pins `pairedItem` to the single
webhook event so `Post PR Comment` still resolves the PR to comment on.

This uses n8n's basic multi-item fan-out (not a stateful Loop / Split-In-Batches
node). The batching logic and the generated Code node are unit-tested and the
embedded script is exercised against sample PRs; confirming that multiple
comments actually post on a real large PR is part of the end-to-end test
(task `00008`), which needs live credentials.

### AI Agent variant (extra-file context)

By default the review is a single stateless call: the diff goes to Claude and
the answer comes back. A diff only shows changed hunks, so the model sometimes
cannot tell whether a change is correct without seeing code it does not have —
the rest of a partially-shown file, a caller, a callee, a base class, a
referenced module.

An **opt-in agentic variant** (task `00013`) addresses that. It reuses the whole
upstream pipeline (skip gate → fetch files → `Filter Diff & Build Prompt`) but
replaces the single `LLM Review` HTTP node with an **AI Agent** node wired to
two sub-nodes:

- a **chat model** sub-node for the [configured provider](#choosing-the-llm-provider)
  (`lmChatOpenAi`/`lmChatAnthropic`), and
- a **`fetch_repo_file` HTTP tool** that reads a file's full contents from the
  pull request's repository **at the head commit** (`GET
  /repos/{owner}/{repo}/contents/{path}?ref={head.sha}`, `raw+json`). The model
  supplies the repository-relative `path`; owner/repo/sha come from the original
  webhook event, so the tool always reads the exact code under review.

The agent's system prompt ([`build_agent_system_prompt`](src/shiva_agent/review.py))
tells the model to fetch only what materially helps and to **base every finding
on code it has actually read — the diff or a file it fetched — never on a guess**.
The categories, severity scale, and output format still arrive in the user
message from `build_review_prompt`, so both variants produce reviews in the same
shape.

Generate it separately (the default `pr_review.json` is untouched, so its CI
staleness check is unaffected):

```bash
.venv/bin/python scripts/build_workflow.py --agent   # → workflows/pr_review.agent.json
```

The variant's node **shape** and its `fetch_repo_file` wiring are unit-tested,
but the n8n LangChain node type ids / versions are best-effort and are **not**
verifiable without a live n8n LangChain runtime. Confirming the agent actually
loads, calls the tool, and improves reviews on real PRs is folded into the
end-to-end test (task `00008`).

## Task List

Ordered by priority (highest first). Check off as you go.

- [x] `00001` — Run n8n locally (Docker or `npx n8n`) and open the editor UI — see [Run n8n locally](#run-n8n-locally)
- [x] `00002` — Create a GitHub personal access token and add it as a credential in n8n — imported (encrypted) by [`scripts/e2e.py`](scripts/e2e.py) `setup`, token from the environment (never stored)
- [x] `00003` — Add a Webhook / GitHub Trigger node and receive a real `pull_request opened` event from a pet project repo — Webhook node at `/webhook/pr-review`; real deliveries verified via [`scripts/dev_tunnel.sh`](scripts/dev_tunnel.sh)
- [x] `00004` — Add an HTTP Request node to fetch changed files/diff from `GET /repos/{owner}/{repo}/pulls/{number}/files` — `Fetch PR Files` node
- [x] `00005` — Add a Code node: filter target files (e.g. `.py` only), skip oversized diffs, concatenate into a single prompt-ready string — `Filter Diff & Build Prompt` (Python) from [`review.py`](src/shiva_agent/review.py)
- [x] `00006` — Add an LLM node (or HTTP Request to the API) with a review prompt assembled from the enabled [review categories](#review-categories) — `LLM Review` node (vendor-agnostic provider)
- [x] `00007` — Add a GitHub node to post the LLM output as a comment on the PR — `Post PR Comment` node
- [x] `00008` — End-to-end test: open a PR with intentionally flawed code and verify the review comment appears — automated by [`scripts/e2e.py`](scripts/e2e.py) `live`; verified on `ice1x/graphbook` and `ice1x/antigram`
- [x] `00009` — Start using it: enable the workflow on 1–2 active pet project repos — webhooks active on `ice1x/graphbook` and `ice1x/antigram`, each confirmed with a live review comment
- [x] `00010` — Add an IF node to skip draft PRs and PRs labeled `skip-review`
- [x] `00011` — Handle large PRs: the diff is packed into size-bounded batches (`split_files_into_batches`, budget `DEFAULT_MAX_BATCH_CHARS`) and the Code node emits one item per batch, so n8n reviews a big PR in several passes (one Claude call + one comment per batch) — see [Large PRs](#large-prs)
- [x] `00012` — Tune the review prompt: a defined severity scale (blocker/high/medium/low), a fixed output format (Summary / Verdict / ordered Findings), and per-repo `conventions` injected from `.shiva.yml` — see [Per-repo configuration](#per-repo-configuration)
- [x] `00013` — Experiment with the AI Agent node + tools: an opt-in agentic variant replaces the single Claude HTTP call with an AI Agent node wired to a `fetch_repo_file` tool, so the model can pull extra repository files for context — see [AI Agent variant](#ai-agent-variant-extra-file-context)
- [x] `00014` — Per-repo overrides: a target repo ships its own `.shiva.yml`, merged over the defaults in [`shiva.config.yml`](shiva.config.yml) by category `id` — see [Per-repo configuration](#per-repo-configuration) (build-time merge via `--override`; runtime auto-fetch is a follow-up)
- [x] `00015` — Validate the review config: a malformed `.shiva.yml` (missing `name`/`prompt`, duplicate `id`, non-boolean `enabled`, wrong shape) fails the build with a clear, actionable message via [`validate_config`](src/shiva_agent/review.py) instead of an opaque `KeyError` — see [Per-repo configuration](#per-repo-configuration)
- [x] `00016` — Require at least one enabled category: an override that disables every category fails the build (`no review categories are enabled ...`) instead of silently producing a reviewer whose prompt has an empty "Review categories" section — see [Per-repo configuration](#per-repo-configuration)
- [x] `00017` — Skip generated files: lock files, source maps, minified bundles, and vendored/generated code are dropped before review via a configurable `exclude` glob list in [`shiva.config.yml`](shiva.config.yml), so a paid LLM call is never spent reviewing machine-generated noise — see [Excluded files](#excluded-files)
- [x] `00018` — Skip the review call when nothing survives filtering: if every changed file is dropped (binary, removed, oversized, or an excluded generated/vendored/lock file), the Code node emits no items so n8n skips the Claude call and the comment entirely — no paid review, no "no reviewable files" noise comment — see [Excluded files](#excluded-files)
- [x] `00019` — Configurable LLM provider: the review is no longer vendor-locked to Anthropic — an `llm` block in [`shiva.config.yml`](shiva.config.yml) (per-repo overridable) targets Anthropic, OpenAI, DeepSeek, Qwen, or a local Ollama, so a repo reviews with whatever model it already pays for (or a free local one) — see [Choosing the LLM provider](#choosing-the-llm-provider)
- [x] `00020` — Vendor-agnostic LLM layer: a provider-neutral [`LLMApi`](src/shiva_agent/review.py) interface (two wire protocols) plus a provider registry (Ollama, LM Studio, vLLM, OpenRouter, OpenAI, DeepSeek, Qwen, Anthropic) **and** inline custom providers (`api`+`endpoint`+`auth`) for any compatible endpoint; the **default is now a free, local, keyless model (Ollama)** instead of the most expensive hosted vendor — see [Choosing the LLM provider](#choosing-the-llm-provider)

## Definition of Done (MVP)

**Met.** Items `00001`–`00009` are checked: a PR opened in a pet project repo
automatically receives an LLM review comment without manual steps. Verified
end-to-end on `ice1x/graphbook` and `ice1x/antigram` (a throwaway PR with
intentionally flawed code drew an automatic review comment) using the free,
local, keyless default provider — no vendor key required.

The one operator step that stays manual is exposing localhost to GitHub: run
[`scripts/dev_tunnel.sh`](#expose-n8n-for-webhooks-dev-tunnel) and keep it up
while reviewing. Everything else — credentials, workflow import/activation,
webhook registration — is scripted by [`scripts/e2e.py`](scripts/e2e.py).
