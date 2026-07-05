# n8n PR Review Agent (MVP)

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
schema. A target repository will be able to override it with its own
`.shiva.yml` at the repo root, merged over the defaults by category `id`
(task `00014`).

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
Fetch PR Files → Filter Diff & Build Prompt (Python Code node) → Claude Review
(`claude-opus-4-8`, adaptive thinking) → Post PR Comment. Before it can run
end-to-end you still need to attach credentials in the n8n UI (task `00002`):
an HTTP Header Auth credential `Authorization: Bearer <GitHub PAT>` on the two
GitHub nodes and `x-api-key: <Anthropic API key>` on the Claude node, plus a
tunnel `WEBHOOK_URL` for real webhooks (task `00003`).

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
- [ ] `00010` — Add an IF node to skip draft PRs and PRs labeled `skip-review`
- [ ] `00011` — Handle large PRs: split diffs per file and review in a Loop node
- [ ] `00012` — Tune the review prompt based on real feedback quality (add repo conventions, severity levels, output format)
- [ ] `00013` — Experiment with the AI Agent node + tools so the model can request extra repo files for context
- [ ] `00014` — Per-repo overrides: let a target repo ship its own `.shiva.yml`, merged over the defaults in [`shiva.config.yml`](shiva.config.yml) by category `id`

## Definition of Done (MVP)

Items `00001`–`00009` are checked: a PR opened in a pet project repo automatically receives an LLM review comment without manual steps.
