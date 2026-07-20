# Running the agent on n8n (the original runtime)

This is how the MVP was built and first proved: a local n8n instance, a
Cloudflare quick tunnel, and the review logic embedded into Python Code nodes.
It still works, and it is the right setup for hacking on the pipeline visually.

For everyday reviewing prefer the [GitHub Action](github-action.md): it needs no
tunnel and no always-on machine. The **operational weakness of this runtime is
that reviews only happen while your laptop is awake with the tunnel up.**

**One more difference.** Output sanitizing (`sanitize_review` — unwrapping a code
fence around the whole review, dropping line anchors that are not in the diff)
runs only on the Action path. The n8n workflow builds its comment body from an
expression on the raw response and the gate Code node cannot see the diff the
model was given, so a fenced answer or an invented `file.ts:0` anchor still
reaches the comment here. Use the Action if that matters.

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
localhost. [`scripts/dev_tunnel.sh`](../scripts/dev_tunnel.sh) opens a keyless
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
[`scripts/e2e.py`](../scripts/e2e.py) so it is not a manual click-through. Its pure
logic (payload/credential/webhook builders, review-comment detection) lives in
[`src/shiva_agent/e2e.py`](../src/shiva_agent/e2e.py) and is unit-tested
([`tests/test_e2e.py`](../tests/test_e2e.py)); the script itself orchestrates the
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

## Workflow as Code

The n8n workflow is generated, not hand-built. Sources of truth:

- [`shiva.config.yml`](../shiva.config.yml) — review categories (enabled ones end up in the prompt), per-repo `conventions`, the [`exclude`](#excluded-files) globs for generated files, and the [`llm`](#choosing-the-llm-provider) provider
- [`src/shiva_agent/review.py`](../src/shiva_agent/review.py) — diff filtering and prompt assembly (unit-tested, embedded verbatim into the Code node)
- [`scripts/build_workflow.py`](../scripts/build_workflow.py) — assembles [`workflows/pr_review.json`](../workflows/pr_review.json)

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

The agent's system prompt ([`build_agent_system_prompt`](../src/shiva_agent/review.py))
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
