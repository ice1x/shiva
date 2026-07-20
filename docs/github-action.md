# Running Shiva as a GitHub Action

The review runs on GitHub's runners, triggered by the target repository's own
`pull_request` event. No tunnel, no n8n, no always-on machine — which is the one
operational weakness the [n8n runtime](n8n.md) never solved.

## Add it to a repository

1. Add a repository secret named `SHIVA_LLM_API_KEY` with the key for the
   provider you intend to use (`Settings → Secrets and variables → Actions`).
2. Commit this workflow:

```yaml
# .github/workflows/shiva-review.yml
name: Shiva Review

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]

permissions:
  contents: read
  pull-requests: write   # required to post the review comment

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4        # so .shiva.yml is on disk
      - uses: ice1x/shiva@main
        with:
          llm-api-key: ${{ secrets.SHIVA_LLM_API_KEY }}
```

3. Optionally add a `.shiva.yml` at the repo root to pick the provider, turn
   categories on/off, and state house rules — see
   [Per-repo configuration](../README.md#per-repo-configuration).

That is the whole setup. The next pull request gets reviewed.

## Inputs

| Input | Default | Purpose |
|---|---|---|
| `llm-api-key` | `""` | Key for the resolved provider. Omit only for a keyless one. |
| `github-token` | `${{ github.token }}` | Reads the diff, posts the comment. The default is enough. |
| `config` | the shipped `shiva.config.yml` | Alternative default config. |
| `python-version` | `3.12` | Python used for the run. |
| `dry-run` | `false` | Fetch and review, but log the comment instead of posting it. |

## Pick a provider a runner can reach

The shipped default is a **local, keyless Ollama** — free, and correct for a
laptop-run n8n. A GitHub runner cannot reach it: nothing listens on that
machine's localhost. The run therefore fails fast with an explicit message
rather than a confusing connection error:

```
error: llm provider 'ollama' points at 'http://host.docker.internal:11434/...',
which a GitHub runner cannot reach — it is local to the machine running n8n.
Set a hosted provider in .shiva.yml (llm.provider: openai | deepseek |
anthropic | openrouter) and pass its key as SHIVA_LLM_API_KEY.
```

So a repo reviewed by the Action names a hosted provider:

```yaml
# .shiva.yml
llm:
  provider: openai
  model: gpt-4o-mini
```

A self-hosted model still works if it is reachable from the runner (a public
endpoint, or a self-hosted runner on the same network) — set `endpoint`.

## What a run does

1. **Gate** — a draft PR or one labelled `skip-review` stops here, before any
   request.
2. **Fetch** — every page of `GET /repos/{repo}/pulls/{n}/files`.
3. **Filter** — binary, removed, oversized, and excluded (lock/minified/vendored)
   files are dropped. If nothing survives, the run ends: no model call, no
   comment.
4. **Batch** — remaining files are packed into size-bounded passes so a large PR
   is reviewed in several prompts instead of one oversized one.
5. **Review** — one model call per pass, using the categories and conventions
   resolved for this repo.
6. **Post** — the answer is sanitized (a code fence wrapping the whole review is
   unwrapped; line numbers that do not exist in the diff are stripped) and posted
   as a PR comment. A pass with nothing to act on posts nothing.

The decisions live in [`src/shiva_agent/action.py`](../src/shiva_agent/action.py)
and are unit-tested with the transport injected; the HTTP shell is
[`scripts/review_pr.py`](../scripts/review_pr.py).

## Try it without posting

`--dry-run` really fetches the diff and really asks the model — it withholds
only the comment and prints it, so you can read a review before trusting it on a
live PR:

```bash
export GITHUB_TOKEN=$(gh auth token)
export SHIVA_LLM_API_KEY=sk-...
python scripts/review_pr.py --repo owner/name --pr 42 --dry-run
```

Credentials are read from the environment only and redacted from every logged
header.

## Cost and permissions

- One model call per review pass (most PRs: one). Lock files, generated code, and
  binaries never reach a paid call.
- `synchronize` is included in the trigger, so each push to an open PR is a new
  review. Drop it from `types:` to review only when the PR opens.
- The default `GITHUB_TOKEN` posts as *github-actions[bot]*. To post under a
  different identity, pass a PAT as `github-token`.
