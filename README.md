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

## Task List

Ordered by priority (highest first). Check off as you go.

- [x] `00001` — Run n8n locally (Docker or `npx n8n`) and open the editor UI — see [Run n8n locally](#run-n8n-locally)
- [ ] `00002` — Create a GitHub personal access token and add it as a credential in n8n
- [ ] `00003` — Add a Webhook / GitHub Trigger node and receive a real `pull_request opened` event from a pet project repo
- [ ] `00004` — Add an HTTP Request node to fetch changed files/diff from `GET /repos/{owner}/{repo}/pulls/{number}/files`
- [ ] `00005` — Add a Code node: filter target files (e.g. `.py` only), skip oversized diffs, concatenate into a single prompt-ready string
- [ ] `00006` — Add an LLM node (or HTTP Request to the API) with a review prompt covering bugs, style, and security issues
- [ ] `00007` — Add a GitHub node to post the LLM output as a comment on the PR
- [ ] `00008` — End-to-end test: open a PR with intentionally flawed code and verify the review comment appears
- [ ] `00009` — Start using it: enable the workflow on 1–2 active pet project repos
- [ ] `00010` — Add an IF node to skip draft PRs and PRs labeled `skip-review`
- [ ] `00011` — Handle large PRs: split diffs per file and review in a Loop node
- [ ] `00012` — Tune the review prompt based on real feedback quality (add repo conventions, severity levels, output format)
- [ ] `00013` — Experiment with the AI Agent node + tools so the model can request extra repo files for context

## Definition of Done (MVP)

Items `00001`–`00009` are checked: a PR opened in a pet project repo automatically receives an LLM review comment without manual steps.
