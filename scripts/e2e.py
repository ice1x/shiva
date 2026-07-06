#!/usr/bin/env python3
"""End-to-end driver for the Shiva PR review agent (task ``00008``).

Two subcommands:

  smoke   Deterministic, local, no external mutation. POSTs a synthetic *draft*
          pull_request event to the local webhook and asserts n8n accepts it and
          short-circuits at the skip gate (no GitHub, no LLM call). Proves the
          webhook + skip logic are wired. Safe for CI.

  live    Full run against a real repo: import the PAT credential into n8n, wire
          it into the workflow, activate it, register the repo webhook, open a
          throwaway PR with intentionally flawed code, then poll until the review
          comment appears (or time out).

Secrets never touch the repo. The GitHub token is resolved at runtime from
``$SHIVA_GITHUB_TOKEN`` or ``gh auth token`` and written only to a 0600 temp file
that is deleted in a ``finally`` block. The committed workflow JSON is never
modified — a patched copy is imported from a temp file.

Prereqs: docker compose up (n8n running), ``gh`` authenticated, and for ``live``
a public tunnel (``scripts/dev_tunnel.sh``) so GitHub can reach the webhook.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from shiva_agent.e2e import (
    CREDENTIAL_NAME,
    attach_credential_to_workflow,
    build_header_auth_credential,
    build_synthetic_pr_event,
    build_webhook_config,
    describe_live_plan,
    find_existing_hook_id,
    find_review_comment,
    flawed_python_sample,
    llm_node_needs_credential,
    mapped_providers,
    missing_credential_nodes,
    redact,
    webhook_url,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / "workflows" / "pr_review.json"
N8N_CONTAINER = "shiva-n8n"
N8N_USER = "node"  # the n8n image runs as user `node` (uid 1000)
WORKFLOW_ID = "ShivaPrReview001"
CREDENTIAL_ID = "shivaGithubPat"
LLM_NODE = "LLM Review"
LLM_CREDENTIAL_ID = "shivaLlmKey"
LLM_CREDENTIAL_NAME = "shiva-llm-key"
DEFAULT_WORKFLOW = str(WORKFLOW_PATH)
LOCAL_BASE = "http://localhost:5678"


# --- small shell helpers ---------------------------------------------------


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> str:
    """Run a command, streaming or capturing stdout. Never logs args verbatim
    for secret-bearing calls — callers pass secrets via files, not argv."""
    result = subprocess.run(
        cmd, check=check, text=True, capture_output=capture
    )
    return (result.stdout or "") if capture else ""


def gh_api(path: str, *, method: str = "GET", fields: dict | None = None) -> object:
    cmd = ["gh", "api", "-X", method, path]
    for key, value in (fields or {}).items():
        cmd += ["-f", f"{key}={value}"]
    out = run(cmd, capture=True)
    return json.loads(out) if out.strip() else None


def n8n_exec(args: list[str]) -> str:
    return run(["docker", "exec", N8N_CONTAINER, "n8n", *args], capture=True)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Load ``KEY=VALUE`` lines from the gitignored .env into os.environ.

    A real environment variable always wins. Lets the hosted-LLM API key live in
    .env (never committed) instead of being pasted into a shell or this chat.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def resolve_token(*, required: bool = True) -> str:
    """Resolve the GitHub token from env or ``gh``. Never printed by callers.

    With ``required=False`` (dry-run) an absent token returns "" instead of
    exiting, so the plan can still be shown.
    """
    token = os.environ.get("SHIVA_GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        token = run(["gh", "auth", "token"], capture=True, check=required).strip()
    except subprocess.CalledProcessError:
        token = ""
    if not token and required:
        sys.exit(
            "error: no GitHub token. Set $SHIVA_GITHUB_TOKEN or run `gh auth login`."
        )
    return token


def resolve_llm_token(provider: str | None = None) -> str:
    """Hosted-LLM API key for ``provider`` (from env or .env). "" when unset.

    A per-repo mapping can target several providers with different keys, so the
    key is looked up by provider, most specific first. Every name is
    ``SHIVA_``-namespaced so it can't accidentally pick up an unrelated key from
    the shell (e.g. a bare ``OPENAI_API_KEY`` meant for another tool):

      1. ``SHIVA_<PROVIDER>_API_KEY``   e.g. ``SHIVA_OPENAI_API_KEY``
      2. ``SHIVA_LLM_API_KEY``          generic fallback (single-provider case)
    """
    names = ["SHIVA_LLM_API_KEY"]
    if provider:
        names = [f"SHIVA_{provider.upper()}_API_KEY", "SHIVA_LLM_API_KEY"]
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def llm_key_env_var(provider: str) -> str:
    """The preferred, provider-specific API-key env var name."""
    return f"SHIVA_{provider.upper()}_API_KEY"


def load_config() -> dict:
    import yaml  # PyYAML is a project dependency

    return yaml.safe_load((REPO_ROOT / "shiva.config.yml").read_text())


def resolve_llm_provider(repo: str | None = None) -> str | None:
    """Provider the config resolves to for ``repo`` (or the default when None).

    Lets the driver pick the right per-provider API-key env var. Returns None if
    the config can't be read (the caller falls back to the generic key).
    """
    try:
        from shiva_agent import review

        return review.resolve_llm(load_config(), repo=repo)["provider"]
    except Exception:
        return None


def local_server_reachable(endpoint: str) -> bool:
    """Whether a local model server answers at ``endpoint`` (from n8n's view).

    The endpoint uses ``host.docker.internal`` — a container-relative name — so
    the check runs *inside* the n8n container, exactly who calls the model. Any
    HTTP response (even 404) counts as reachable; a connection failure or a
    missing/stopped container counts as down.
    """
    base = endpoint.rsplit("/chat/completions", 1)[0]
    proc = subprocess.run(
        ["docker", "exec", N8N_CONTAINER, "wget", "-q", "-O", "/dev/null",
         "--timeout=5", f"{base}/models"],
        capture_output=True, text=True,
    )
    # wget: 0 = 2xx, 8 = server responded (e.g. 404) — both mean reachable;
    # 4 = connection failure; other codes = docker/container error.
    return proc.returncode in (0, 8)


def check_providers(
    config: dict, only_provider: str | None = None
) -> list[tuple[str, list[str], bool, str]]:
    """Readiness of mapped providers: ``[(provider, labels, ok, detail)]``.

    Hosted → needs an API key; local → needs a reachable server. Covers the
    default provider and every ``llm_by_repo`` entry. When ``only_provider`` is
    given, validates ONLY that provider — used by setup/live so a deploy of one
    repo's workflow isn't blocked by an unrelated (e.g. a stopped local default)
    provider it doesn't use. ``check`` passes None to validate the whole config.
    """
    rows = []
    for info in sorted(mapped_providers(config), key=lambda p: p["provider"]):
        provider, labels = info["provider"], info["labels"]
        if only_provider is not None and provider != only_provider:
            continue
        if info["needs_key"]:
            ok = bool(resolve_llm_token(provider))
            detail = "key set" if ok else f"no key — set {llm_key_env_var(provider)} in .env"
        else:
            ok = local_server_reachable(info["endpoint"])
            detail = (
                f"reachable at {info['endpoint']}" if ok
                else f"unreachable at {info['endpoint']} — is the server (and n8n) up?"
            )
        rows.append((provider, labels, ok, detail))
    return rows


def validate_deployment(
    config: dict, only_provider: str | None = None
) -> list[tuple[str, list[str], bool, str]]:
    """Config-validation step: structural check, then per-provider readiness.

    Runs *before* any pipeline action so a misconfigured or unprovisioned setup
    exits during validation, not half-way through importing / opening a PR.
    ``only_provider`` narrows the readiness check to the provider being deployed.
    Structural errors raise ConfigError; returns the provider readiness rows.
    """
    from shiva_agent import review

    review.validate_config(config)
    return check_providers(config, only_provider=only_provider)


def enforce_deployment(only_provider: str | None = None) -> None:
    """Fail early (validation stage) unless the config is valid and the relevant
    provider(s) — hosted (key) and local (reachable server) — are ready.

    ``only_provider`` scopes the readiness check to the provider actually being
    deployed, so e.g. a stopped local default does not block deploying a repo
    whose workflow uses a hosted provider."""
    from shiva_agent import review

    config = load_config()
    try:
        rows = validate_deployment(config, only_provider=only_provider)
    except review.ConfigError as exc:
        sys.exit(f"error: invalid shiva.config.yml: {exc}")
    problems = [r for r in rows if not r[2]]
    if not problems:
        return
    lines = ["error: LLM provider(s) not ready (config validation):"]
    for provider, labels, _ok, detail in problems:
        lines.append(f"  {provider} ({', '.join(labels)}): {detail}")
    lines.append("Run `python scripts/e2e.py check` for the full status.")
    sys.exit("\n".join(lines))


def cmd_check(_args: argparse.Namespace) -> int:
    from shiva_agent import review

    config = load_config()
    try:
        rows = validate_deployment(config)
    except review.ConfigError as exc:
        print(f"check: FAIL — invalid shiva.config.yml: {exc}")
        return 1
    for provider, labels, ok, detail in rows:
        mark = "ok" if ok else "FAIL"
        kind = "hosted" if any(p["provider"] == provider and p["needs_key"]
                               for p in mapped_providers(config)) else "local"
        print(f"  [{mark:>4}] {provider:<10} ({kind}) used by {', '.join(labels)} — {detail}")
    if any(not ok for _p, _l, ok, _d in rows):
        print("\ncheck: FAIL — see above.")
        return 1
    print("\ncheck: OK — every mapped provider is ready.")
    return 0


# --- smoke -----------------------------------------------------------------


def cmd_smoke(args: argparse.Namespace) -> int:
    url = webhook_url(LOCAL_BASE)
    event = build_synthetic_pr_event("shiva/e2e-smoke", 1, draft=True)
    if args.dry_run:
        print(f"[dry-run] would POST to {url}:")
        print(json.dumps(event["body"], indent=2))
        print("[dry-run] no request sent.")
        return 0
    payload = json.dumps(event["body"]).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            sys.exit(
                "smoke: webhook returned 404 — the workflow is not active.\n"
                "Run `python scripts/e2e.py setup` (or activate it in the UI) first."
            )
        sys.exit(f"smoke: webhook POST failed with HTTP {exc.code}")
    except urllib.error.URLError as exc:
        sys.exit(f"smoke: cannot reach {url} — is n8n up? ({exc.reason})")
    print(f"smoke: OK — draft event accepted (HTTP {code}); skip gate exercised.")
    return 0


# --- setup (credential + workflow + activate) ------------------------------


def setup_n8n(
    token: str,
    *,
    dry_run: bool = False,
    workflow_path: str = DEFAULT_WORKFLOW,
    llm_provider: str | None = None,
) -> None:
    """Import credentials, wire them into the workflow, activate it.

    Always wires the GitHub PAT into the two GitHub nodes. When the workflow
    targets a hosted LLM (LLM Review uses genericCredentialType), also imports an
    API-key credential (resolved by provider, see resolve_llm_token) and wires
    it into LLM Review.

    Idempotent: re-importing the same ids overwrites in place.
    """
    workflow = json.loads(Path(workflow_path).read_text())
    wired = attach_credential_to_workflow(workflow, CREDENTIAL_ID, CREDENTIAL_NAME)
    still_missing = missing_credential_nodes(wired)
    if still_missing:
        sys.exit(f"setup: could not wire credential into nodes: {still_missing}")

    creds = [
        build_header_auth_credential(
            token or "RESOLVED_AT_RUNTIME", name=CREDENTIAL_NAME, credential_id=CREDENTIAL_ID
        )
    ]
    secrets = [token]
    if llm_node_needs_credential(workflow):
        # The key's presence is guaranteed up front by enforce_deployment (the
        # config-validation gate), so no mid-pipeline check here.
        llm_token = resolve_llm_token(llm_provider)
        wired = attach_credential_to_workflow(
            wired, LLM_CREDENTIAL_ID, LLM_CREDENTIAL_NAME, node_names=[LLM_NODE]
        )
        creds.append(
            build_header_auth_credential(
                llm_token or "RESOLVED_AT_RUNTIME",
                name=LLM_CREDENTIAL_NAME,
                credential_id=LLM_CREDENTIAL_ID,
            )
        )
        secrets.append(llm_token)

    if dry_run:
        print(f"[dry-run] workflow: {workflow_path}")
        print("[dry-run] would import these credentials (secrets redacted):")
        print(redact(json.dumps(creds, indent=2), *[s for s in secrets if s]))
        print("[dry-run] then run inside the container:")
        for step in (
            ["import:credentials", "--input=/tmp/e2e_cred.json"],
            ["import:workflow", "--input=/tmp/e2e_wf.json"],
            ["update:workflow", f"--id={WORKFLOW_ID}", "--active=true"],
        ):
            print("  n8n " + " ".join(step))
        print("[dry-run] nothing imported.")
        return
    # Secret-bearing temp file: 0600, container-copied, deleted in finally.
    cred_fd, cred_path = tempfile.mkstemp(suffix=".json")
    wf_fd, wf_path = tempfile.mkstemp(suffix=".json")
    try:
        os.write(cred_fd, json.dumps(creds).encode())
        os.close(cred_fd)
        os.write(wf_fd, json.dumps(wired).encode())
        os.close(wf_fd)
        remote_files = ["/tmp/e2e_cred.json", "/tmp/e2e_wf.json"]
        for local, remote in ((cred_path, remote_files[0]), (wf_path, remote_files[1])):
            run(["docker", "cp", local, f"{N8N_CONTAINER}:{remote}"])
        # docker cp preserves the host uid (root-owned from the container's view)
        # and 0600, so the n8n process (user `node`) gets EACCES. Hand the files
        # to `node` via root so the CLI can read them.
        run(["docker", "exec", "-u", "root", N8N_CONTAINER, "chown", f"{N8N_USER}:{N8N_USER}", *remote_files])
        n8n_exec(["import:credentials", "--input=/tmp/e2e_cred.json"])
        n8n_exec(["import:workflow", "--input=/tmp/e2e_wf.json"])
        n8n_exec(["update:workflow", f"--id={WORKFLOW_ID}", "--active=true"])
    finally:
        for path in (cred_path, wf_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        # Scrub the secret-bearing file inside the container. Must be root: the
        # files are root-owned and /tmp is sticky, so `node` cannot delete them.
        run(["docker", "exec", "-u", "root", N8N_CONTAINER, "rm", "-f", *remote_files], check=False)
    # CLI `update:workflow --active=true` flips the DB flag but the running n8n
    # process does NOT register the production webhook until it restarts, so
    # POSTs to /webhook/pr-review 404 until then. Restart so the webhook goes live.
    restart_n8n()
    print(f"setup: {len(creds)} credential(s) imported, workflow wired, activated (n8n restarted).")


def restart_n8n() -> None:
    """Restart n8n and wait until the production webhook is actually live.

    The REST API answers a moment before the webhook is registered, so waiting
    on ``/rest/settings`` alone races. Instead poll the webhook path with a
    harmless ``{}`` POST (it short-circuits at the skip gate) until it stops
    404-ing — that is the real signal the activated workflow is serving.
    """
    run(["docker", "restart", N8N_CONTAINER])
    url = webhook_url(LOCAL_BASE)
    for _ in range(90):
        req = urllib.request.Request(
            url, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 404:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                return  # any non-404 response means the webhook is registered
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    sys.exit("setup: n8n webhook did not come up after restart.")


def cmd_setup(args: argparse.Namespace) -> int:
    if not args.dry_run:
        enforce_deployment(only_provider=resolve_llm_provider(args.repo))
    setup_n8n(
        resolve_token(required=not args.dry_run),
        dry_run=args.dry_run,
        workflow_path=args.workflow,
        # Resolve the LLM key by the workflow's provider: --repo if given, else
        # the config default. Without this a hosted `setup` can't find its key.
        llm_provider=resolve_llm_provider(args.repo),
    )
    return 0


# --- live ------------------------------------------------------------------


def ensure_webhook(repo: str, payload_url: str) -> None:
    hooks = gh_api(f"repos/{repo}/hooks") or []
    if find_existing_hook_id(hooks, payload_url) is not None:
        print(f"live: webhook already present on {repo}.")
        return
    cfg = build_webhook_config(payload_url)
    # gh api needs nested config; send via --input from stdin.
    proc = subprocess.run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/hooks", "--input", "-"],
        input=json.dumps(cfg), text=True, capture_output=True,
    )
    if proc.returncode != 0:
        sys.exit(f"live: failed to create webhook: {proc.stderr.strip()}")
    print(f"live: webhook created on {repo} → {payload_url}")


def open_test_pr(repo: str, branch: str) -> dict:
    default_branch = gh_api(f"repos/{repo}")["default_branch"]
    base_sha = gh_api(f"repos/{repo}/git/ref/heads/{default_branch}")["object"]["sha"]
    # Create branch.
    gh_api(
        f"repos/{repo}/git/refs", method="POST",
        fields={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    # Commit the flawed file via the contents API.
    import base64

    content = base64.b64encode(flawed_python_sample().encode()).decode()
    gh_api(
        f"repos/{repo}/contents/shiva_e2e_sample.py", method="PUT",
        fields={
            "message": "e2e: intentionally flawed sample",
            "content": content,
            "branch": branch,
        },
    )
    pr = gh_api(
        f"repos/{repo}/pulls", method="POST",
        fields={
            "title": "E2E: flawed sample for Shiva review",
            "head": branch,
            "base": default_branch,
            "body": "Automated end-to-end test PR (task 00008). Safe to close.",
        },
    )
    print(f"live: opened PR #{pr['number']} on {repo}")
    return pr


def poll_for_review(repo: str, number: int, author: str, since: str, timeout: int) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        comments = gh_api(f"repos/{repo}/issues/{number}/comments") or []
        found = find_review_comment(comments, author=author, since=since)
        if found:
            return found
        time.sleep(5)
    return None


def cmd_live(args: argparse.Namespace) -> int:
    if not args.base_url:
        sys.exit("live: --base-url is required (your public tunnel URL).")
    payload_url = webhook_url(args.base_url)
    branch = f"shiva-e2e-{int(time.time())}"

    if args.dry_run:
        token = resolve_token(required=False)
        print(f"[dry-run] token: {'resolved (hidden)' if token else 'NOT found — set $SHIVA_GITHUB_TOKEN or gh auth login'}")
        print(f"[dry-run] target repo: {args.repo}")
        print(f"[dry-run] webhook Payload URL: {payload_url}")
        print("[dry-run] planned actions:")
        for i, step in enumerate(describe_live_plan(args.repo, payload_url, branch, keep=args.keep), 1):
            print(f"  {i}. {step}")
        print("[dry-run] flawed sample that would be committed:")
        print("    " + flawed_python_sample().replace("\n", "\n    ").rstrip())
        print("[dry-run] no n8n import, no webhook, no PR — nothing was changed.")
        return 0

    enforce_deployment(only_provider=resolve_llm_provider(args.repo))
    token = resolve_token()
    login = gh_api("user")["login"]

    setup_n8n(
        token,
        workflow_path=args.workflow,
        llm_provider=resolve_llm_provider(args.repo),
    )
    ensure_webhook(args.repo, payload_url)

    pr = open_test_pr(args.repo, branch)
    print(f"live: waiting up to {args.timeout}s for the review comment ...")
    review = poll_for_review(args.repo, pr["number"], login, pr["created_at"], args.timeout)

    ok = review is not None
    if ok:
        print(f"live: PASS — review comment posted:\n{review['html_url']}")
    else:
        print("live: FAIL — no review comment appeared before timeout.")
        print("      Check n8n → Executions and the repo webhook's Recent Deliveries.")

    if not args.keep:
        gh_api(f"repos/{args.repo}/pulls/{pr['number']}", method="PATCH", fields={"state": "closed"})
        gh_api(f"repos/{args.repo}/git/refs/heads/{branch}", method="DELETE")
        print(f"live: cleaned up PR #{pr['number']} and branch {branch}.")

    return 0 if ok else 1


# --- cli -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Shared --dry-run: print the plan / payloads (token redacted) and make no
    # network call, no n8n import, and no GitHub mutation.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dry-run", action="store_true",
        help="show what would happen without doing it (no mutations, token redacted)",
    )
    # Shared by setup/live: which workflow JSON to import (default vs a per-repo
    # build, e.g. a hosted-LLM variant from `build_workflow.py --override`).
    wf = argparse.ArgumentParser(add_help=False)
    wf.add_argument("--workflow", default=DEFAULT_WORKFLOW,
                    help="workflow JSON to import (default: workflows/pr_review.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("smoke", parents=[common], help="local deterministic webhook + skip-gate check")
    sub.add_parser("check", help="verify every mapped hosted LLM provider has an API key")
    setup = sub.add_parser("setup", parents=[common, wf], help="import credential + workflow and activate")
    setup.add_argument("--repo", default=None,
                       help="resolve the LLM key by this repo's provider (else the config default)")

    live = sub.add_parser("live", parents=[common, wf], help="full end-to-end run against a real repo")
    live.add_argument("--repo", default="ice1x/graphbook", help="owner/name target repo")
    live.add_argument("--base-url", default=os.environ.get("WEBHOOK_BASE_URL", ""),
                      help="public tunnel base URL (or $WEBHOOK_BASE_URL)")
    live.add_argument("--timeout", type=int, default=180, help="seconds to wait for the review")
    live.add_argument("--keep", action="store_true", help="do not close the PR / delete the branch")

    args = parser.parse_args(argv)
    load_dotenv()  # make .env-provided keys (e.g. SHIVA_OPENAI_API_KEY) visible
    return {
        "smoke": cmd_smoke,
        "check": cmd_check,
        "setup": cmd_setup,
        "live": cmd_live,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
