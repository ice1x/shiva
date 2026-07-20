# Migrating off n8n (design sketch)

n8n gave this project a fast, visual MVP with built-in webhook handling,
credential storage, and execution logs. But the actual pipeline is small — a
webhook receiver plus seven nodes, and the real logic already lives in
unit-tested Python (`src/shiva_agent/review.py`). n8n is heavier than this needs.
**Option B below is now implemented** — see [github-action.md](github-action.md).
This note is kept as the reasoning that led there, and as the map of what is
left of n8n.

## What n8n does here (and what would replace it)

The runtime is: receive a `pull_request` webhook → skip gate → fetch PR files →
filter + build prompt → call the LLM → post a comment. All of that is HTTP glue
around `review.py`.

| n8n piece | Replacement |
|---|---|
| Webhook node + tunnel | an HTTP endpoint on a public host, or GitHub's own delivery |
| Code nodes (skip/filter/prompt) | `review.py` imported directly (no embedding, no sandbox) |
| HTTP nodes (fetch/LLM/comment) | `httpx`/`requests` calls |
| Credential store | env vars / the platform's secret store |
| Executions log | app logs |

## Two realistic targets

**A. A small Python service (FastAPI/Flask).** ~150 lines: one webhook route
that verifies the signature, runs `review.py`, and posts the comment. Deployed on
a VPS / container host. Removes the n8n containers and the Python-runner sandbox
(so `match_glob` could go back to `fnmatch`), but still needs a public host and
webhook management.

**B. A GitHub Action (implemented — this is the default runtime now).** A workflow in each target
repo (`.github/workflows/shiva-review.yml`) triggered on `pull_request`, running
`review.py` on GitHub's runners and posting the comment via `GITHUB_TOKEN` or a
bot token. This removes n8n **and** the tunnel **and** the dependency on the
user's always-on machine — the biggest operational weakness today (see the
"local deployment constraint"). Trade-off: logic now lives in the target repo,
and each repo opts in with a small workflow file.

## Where drevo fits (it is NOT the engine)

drevo is a knowledge graph (Neo4j/Bolt + Cypher), not a workflow orchestrator —
it does not receive webhooks, run HTTP requests, or post comments, so the
pipeline cannot "migrate to drevo". Its role is the **knowledge / feedback
layer**: store the review-feedback log (`data/review_feedback.jsonl`), per-repo
conventions, and — at L2 — power retrieval-augmented review (inject relevant past
rejections into the prompt for the current PR). It complements whichever engine
runs the pipeline.

## Recommendation

If always-on matters, a **GitHub Action** is the cleanest way off n8n for this
project; `review.py` is reused unchanged. Keep drevo as the knowledge layer, not
the runtime.
