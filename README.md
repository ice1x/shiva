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
[`merge_config`](src/shiva_agent/review.py). Because review categories are
resolved and embedded into the Code node at build time, a per-repo workflow is
produced by pointing the generator at the override file:

```bash
.venv/bin/python scripts/build_workflow.py \
    --override path/to/target-repo/.shiva.yml \
    -o workflows/pr_review.<repo>.json
```

Auto-fetching `.shiva.yml` from the target repo at run time (one shared workflow
for all repos) is a future step: it needs YAML parsing inside the n8n Python
Code node, which the stock native runner does not provide.

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

Compose starts two containers: `shiva-n8n` (the editor/engine) and
`shiva-n8n-runners` (the task-runner sidecar that executes Code-node
scripts). The sidecar is required: the stock n8n image ships no Python, so
without it every Python Code node fails with "Python runner unavailable".
Both images must be on the same version.

## Workflow as Code

The n8n workflow is generated, not hand-built. Sources of truth:

- [`shiva.config.yml`](shiva.config.yml) — review categories (enabled ones end up in the prompt)
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
Fetch PR Files → Filter Diff & Build Prompt (Python Code node) → Claude Review
(`claude-opus-4-8`, adaptive thinking) → Post PR Comment. Draft PRs and PRs
labeled `skip-review` end at the IF gate without any GitHub or LLM calls
(task `00010`). Before it can run
end-to-end you still need to attach credentials in the n8n UI (task `00002`):
an HTTP Header Auth credential `Authorization: Bearer <GitHub PAT>` on the two
GitHub nodes and `x-api-key: <Anthropic API key>` on the Claude node, plus a
tunnel `WEBHOOK_URL` for real webhooks (task `00003`).

### Large PRs

A big pull request is not squeezed into one oversized prompt (task `00011`).
After filtering, `Filter Diff & Build Prompt` packs the kept files into
size-bounded batches with `split_files_into_batches` — greedily, in order,
keeping each batch's combined patch length within `DEFAULT_MAX_BATCH_CHARS`
(45k; a single file that alone exceeds the budget still gets its own pass and
is never dropped). The Code node then returns **one item per batch**, so n8n
fans out natively: one `Claude Review` call and one `Post PR Comment` per
batch. Each prompt is told which part it is (`review part i of N`) so the
model scopes findings to the files shown instead of flagging the split-off
files as missing, and every emitted item pins `pairedItem` to the single
webhook event so `Post PR Comment` still resolves the PR to comment on.

This uses n8n's basic multi-item fan-out (not a stateful Loop / Split-In-Batches
node). The batching logic and the generated Code node are unit-tested and the
embedded script is exercised against sample PRs; confirming that multiple
comments actually post on a real large PR is part of the end-to-end test
(task `00008`), which needs live credentials.

## Task List

Ordered by priority (highest first). Check off as you go.

- [x] `00001` — Run n8n locally (Docker or `npx n8n`) and open the editor UI — see [Run n8n locally](#run-n8n-locally)
- [ ] `00002` — Create a GitHub personal access token and add it as a credential in n8n
- [ ] `00003` — Add a Webhook / GitHub Trigger node and receive a real `pull_request opened` event from a pet project repo
- [ ] `00004` — Add an HTTP Request node to fetch changed files/diff from `GET /repos/{owner}/{repo}/pulls/{number}/files`
- [ ] `00005` — Add a Code node: filter target files (e.g. `.py` only), skip oversized diffs, concatenate into a single prompt-ready string
- [ ] `00006` — Add an LLM node (or HTTP Request to the API) with a review prompt assembled from the enabled [review categories](#review-categories)
- [ ] `00007` — Add a GitHub node to post the LLM output as a comment on the PR
- [ ] `00008` — End-to-end test: open a PR with intentionally flawed code and verify the review comment appears
- [ ] `00009` — Start using it: enable the workflow on 1–2 active pet project repos
- [x] `00010` — Add an IF node to skip draft PRs and PRs labeled `skip-review`
- [x] `00011` — Handle large PRs: the diff is packed into size-bounded batches (`split_files_into_batches`, budget `DEFAULT_MAX_BATCH_CHARS`) and the Code node emits one item per batch, so n8n reviews a big PR in several passes (one Claude call + one comment per batch) — see [Large PRs](#large-prs)
- [x] `00012` — Tune the review prompt: a defined severity scale (blocker/high/medium/low), a fixed output format (Summary / Verdict / ordered Findings), and per-repo `conventions` injected from `.shiva.yml` — see [Per-repo configuration](#per-repo-configuration)
- [ ] `00013` — Experiment with the AI Agent node + tools so the model can request extra repo files for context
- [x] `00014` — Per-repo overrides: a target repo ships its own `.shiva.yml`, merged over the defaults in [`shiva.config.yml`](shiva.config.yml) by category `id` — see [Per-repo configuration](#per-repo-configuration) (build-time merge via `--override`; runtime auto-fetch is a follow-up)

## Definition of Done (MVP)

Items `00001`–`00009` are checked: a PR opened in a pet project repo automatically receives an LLM review comment without manual steps.
