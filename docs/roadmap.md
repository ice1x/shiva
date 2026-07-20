# Roadmap and delivery log

Ordered by priority (highest first). Check off as you go.

- [x] `00001` — Run n8n locally (Docker or `npx n8n`) and open the editor UI — see [Run n8n locally](#run-n8n-locally)
- [x] `00002` — Create a GitHub personal access token and add it as a credential in n8n — imported (encrypted) by [`scripts/e2e.py`](../scripts/e2e.py) `setup`, token from the environment (never stored)
- [x] `00003` — Add a Webhook / GitHub Trigger node and receive a real `pull_request opened` event from a pet project repo — Webhook node at `/webhook/pr-review`; real deliveries verified via [`scripts/dev_tunnel.sh`](../scripts/dev_tunnel.sh)
- [x] `00004` — Add an HTTP Request node to fetch changed files/diff from `GET /repos/{owner}/{repo}/pulls/{number}/files` — `Fetch PR Files` node
- [x] `00005` — Add a Code node: filter target files (e.g. `.py` only), skip oversized diffs, concatenate into a single prompt-ready string — `Filter Diff & Build Prompt` (Python) from [`review.py`](../src/shiva_agent/review.py)
- [x] `00006` — Add an LLM node (or HTTP Request to the API) with a review prompt assembled from the enabled [review categories](#review-categories) — `LLM Review` node (vendor-agnostic provider)
- [x] `00007` — Add a GitHub node to post the LLM output as a comment on the PR — `Post PR Comment` node
- [x] `00008` — End-to-end test: open a PR with intentionally flawed code and verify the review comment appears — automated by [`scripts/e2e.py`](../scripts/e2e.py) `live`; verified on `ice1x/graphbook` and `ice1x/antigram`
- [x] `00009` — Start using it: enable the workflow on 1–2 active pet project repos — webhooks active on `ice1x/graphbook` and `ice1x/antigram`, each confirmed with a live review comment
- [x] `00010` — Add an IF node to skip draft PRs and PRs labeled `skip-review`
- [x] `00011` — Handle large PRs: the diff is packed into size-bounded batches (`split_files_into_batches`, budget `DEFAULT_MAX_BATCH_CHARS`) and the Code node emits one item per batch, so n8n reviews a big PR in several passes (one Claude call + one comment per batch) — see [Large PRs](#large-prs)
- [x] `00012` — Tune the review prompt: a defined severity scale (blocker/high/medium/low), a fixed output format (Summary / Verdict / ordered Findings), and per-repo `conventions` injected from `.shiva.yml` — see [Per-repo configuration](#per-repo-configuration)
- [x] `00013` — Experiment with the AI Agent node + tools: an opt-in agentic variant replaces the single Claude HTTP call with an AI Agent node wired to a `fetch_repo_file` tool, so the model can pull extra repository files for context — see [AI Agent variant](#ai-agent-variant-extra-file-context)
- [x] `00014` — Per-repo overrides: a target repo ships its own `.shiva.yml`, merged over the defaults in [`shiva.config.yml`](../shiva.config.yml) by category `id` — see [Per-repo configuration](#per-repo-configuration) (build-time merge via `--override`; runtime auto-fetch is a follow-up)
- [x] `00015` — Validate the review config: a malformed `.shiva.yml` (missing `name`/`prompt`, duplicate `id`, non-boolean `enabled`, wrong shape) fails the build with a clear, actionable message via [`validate_config`](../src/shiva_agent/review.py) instead of an opaque `KeyError` — see [Per-repo configuration](#per-repo-configuration)
- [x] `00016` — Require at least one enabled category: an override that disables every category fails the build (`no review categories are enabled ...`) instead of silently producing a reviewer whose prompt has an empty "Review categories" section — see [Per-repo configuration](#per-repo-configuration)
- [x] `00017` — Skip generated files: lock files, source maps, minified bundles, and vendored/generated code are dropped before review via a configurable `exclude` glob list in [`shiva.config.yml`](../shiva.config.yml), so a paid LLM call is never spent reviewing machine-generated noise — see [Excluded files](#excluded-files)
- [x] `00018` — Skip the review call when nothing survives filtering: if every changed file is dropped (binary, removed, oversized, or an excluded generated/vendored/lock file), the Code node emits no items so n8n skips the Claude call and the comment entirely — no paid review, no "no reviewable files" noise comment — see [Excluded files](#excluded-files)
- [x] `00019` — Configurable LLM provider: the review is no longer vendor-locked to Anthropic — an `llm` block in [`shiva.config.yml`](../shiva.config.yml) (per-repo overridable) targets Anthropic, OpenAI, DeepSeek, Qwen, or a local Ollama, so a repo reviews with whatever model it already pays for (or a free local one) — see [Choosing the LLM provider](#choosing-the-llm-provider)
- [x] `00020` — Vendor-agnostic LLM layer: a provider-neutral [`LLMApi`](../src/shiva_agent/review.py) interface (two wire protocols) plus a provider registry (Ollama, LM Studio, vLLM, OpenRouter, OpenAI, DeepSeek, Qwen, Anthropic) **and** inline custom providers (`api`+`endpoint`+`auth`) for any compatible endpoint; the **default is now a free, local, keyless model (Ollama)** instead of the most expensive hosted vendor — see [Choosing the LLM provider](#choosing-the-llm-provider)

- [x] `00021` — Run without n8n, a tunnel, or an always-on machine: the pipeline is packaged as a composite [GitHub Action](github-action.md) (`action.yml`) driven by [`action.py`](../src/shiva_agent/action.py) and [`review_pr.py`](../scripts/review_pr.py), so a target repo reviews its own PRs on GitHub's runners and reads `.shiva.yml` fresh on every run
- [x] `00022` — Make the posted review trustworthy: the model's answer is sanitized before posting — a code fence wrapping the whole review is unwrapped, and a line anchor that does not exist in the diff is stripped while the path is kept (`sanitize_review`, [`review.py`](../src/shiva_agent/review.py)) — after real reviews cited `file.ts:0`

## Definition of Done (MVP)

**Met.** Items `00001`–`00009`: a PR opened in a pet project repo automatically
receives an LLM review comment without manual steps. Verified end-to-end on
`ice1x/graphbook` and `ice1x/antigram` (a throwaway PR with intentionally flawed
code drew an automatic review comment).

The MVP's remaining operational weakness — reviews happened only while a laptop
was awake with [`scripts/dev_tunnel.sh`](../scripts/dev_tunnel.sh) up — is closed
by `00021`: on the GitHub Action runtime there is no tunnel and no host to keep
alive. The n8n runtime stays supported for visual work on the pipeline.

## Next

- [ ] Post findings as review comments on the diff lines, not one issue comment
- [ ] Skip files already reviewed in an earlier pass on the same PR (`synchronize` re-reviews everything today)
- [ ] Publish a tagged release so target repos can pin `ice1x/shiva@v1` instead of `@main`
