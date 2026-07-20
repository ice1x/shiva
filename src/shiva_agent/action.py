"""GitHub Action runtime for the review agent — the n8n-free execution path.

The original MVP ran the pipeline inside n8n, which meant the review only
happened while a laptop was awake with `scripts/dev_tunnel.sh` up. This module
is the same pipeline expressed as plain functions, so it can run on a GitHub
runner triggered by the target repository's own `pull_request` event: no tunnel,
no always-on host, no n8n.

Everything here is pure — it *describes* HTTP requests and *decides* what to
post. The actual sockets live in `scripts/review_pr.py`, which keeps this
testable without a network and without secrets.
"""

from __future__ import annotations

from .review import (
    DEFAULT_MAX_BATCH_CHARS,
    DEFAULT_MAX_PATCH_CHARS,
    LLM_APIS,
    PROMPT_SENTINEL,
    build_review_prompt,
    extract_review_text,
    filter_files,
    llm_auth,
    review_has_action_items,
    sanitize_review,
    should_skip_pr,
    split_files_into_batches,
)

GITHUB_API = "https://api.github.com"
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
PER_PAGE = 100
# GitHub caps /pulls/{n}/files at 3000 files; 30 pages covers it and bounds the
# loop if a response ever lies about being full.
MAX_FILE_PAGES = 30

# Env var the workflow puts the provider key in (see action.yml).
LLM_KEY_ENV = "SHIVA_LLM_API_KEY"

# Hosts that exist only on the operator's machine: reachable from a laptop-run
# n8n, never from a GitHub runner.
LOCAL_HOSTS = ("host.docker.internal", "localhost", "127.0.0.1", "0.0.0.0", "::1")

FOOTER = "\n\n---\n<sub>🕉 Reviewed by [Shiva](https://github.com/ice1x/shiva)%s</sub>"


class ActionError(RuntimeError):
    """A misconfiguration the operator must fix; reported without a traceback."""


def pull_request_from_event(payload):
    """Return the `pull_request` object from a webhook event payload."""
    pr = (payload or {}).get("pull_request")
    if not pr:
        raise ActionError(
            "the event payload has no 'pull_request' — trigger the workflow on "
            "'pull_request', not on issues or pushes"
        )
    return pr


def _github_headers(token):
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Content-Type": "application/json",
    }


def pr_files_request(repo, number, token, page=1):
    """Describe the request for one page of a PR's changed files."""
    return {
        "method": "GET",
        "url": f"{GITHUB_API}/repos/{repo}/pulls/{number}/files?per_page={PER_PAGE}&page={page}",
        "headers": _github_headers(token),
        "body": None,
    }


def comment_request(repo, number, body, token):
    """Describe the request that posts the review as a PR comment."""
    return {
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
        "headers": _github_headers(token),
        "body": {"body": body},
    }


def require_runner_reachable(llm):
    """Fail early when the resolved provider only exists on the operator's machine.

    The keyless local default (Ollama) is right for a laptop-run n8n and wrong on
    a GitHub runner, where nothing listens on localhost. Catching it here turns a
    confusing connection-refused deep in the run into one actionable message.
    """
    endpoint = llm.get("endpoint") or ""
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if host in LOCAL_HOSTS:
        raise ActionError(
            f"llm provider {llm.get('provider')!r} points at {endpoint!r}, which a "
            "GitHub runner cannot reach — it is local to the machine running n8n. "
            "Set a hosted provider in .shiva.yml (llm.provider: openai | deepseek | "
            f"anthropic | openrouter) and pass its key as {LLM_KEY_ENV}."
        )
    return None


def llm_request(llm, prompt, api_key=None):
    """Describe the review call for the resolved provider, prompt embedded."""
    api = LLM_APIS[llm["api"]]
    body = api.request_body(llm["model"], temperature=llm.get("temperature", 0))
    for message in body.get("messages", []):
        if message.get("content") == PROMPT_SENTINEL:
            message["content"] = prompt
    headers = {h["name"]: h["value"] for h in api.http_headers()}
    header, _hint = llm_auth(llm.get("auth", "none"))
    if header:
        if not api_key:
            raise ActionError(
                f"llm provider {llm.get('provider')!r} needs an API key — set the "
                f"{LLM_KEY_ENV} secret in the repository running this workflow"
            )
        headers[header] = "Bearer " + api_key if header == "Authorization" else api_key
    return {"method": "POST", "url": llm["endpoint"], "headers": headers, "body": body}


def review_passes(
    files,
    categories,
    conventions="",
    exclude_globs=None,
    max_patch_chars=DEFAULT_MAX_PATCH_CHARS,
    max_batch_chars=DEFAULT_MAX_BATCH_CHARS,
):
    """Plan the review: reviewable files, batched, each with its prompt.

    Mirrors what the n8n Code node does — filter (binary/removed/oversized/
    excluded), pack into size-bounded batches for a large PR, and build one
    prompt per batch. Returns `[]` when nothing survives filtering, so the caller
    spends no tokens and posts no noise comment.
    """
    kept = filter_files(
        files, max_patch_chars=max_patch_chars, exclude_globs=exclude_globs
    )
    if not kept:
        return []
    batches = split_files_into_batches(kept, max_batch_chars=max_batch_chars)
    return [
        {
            "prompt": build_review_prompt(
                categories, batch, conventions=conventions, part=(i + 1, len(batches))
            ),
            "files": batch,
            "part": (i + 1, len(batches)),
        }
        for i, batch in enumerate(batches)
    ]


def should_skip_event(event):
    """Whether this event must not be reviewed (draft PR, opt-out label, …).

    Defers to `should_skip_pr`, the same gate the n8n workflow uses. On a runner
    the workflow's own `types:` filter has already decided the event qualifies,
    and a manually dispatched run carries no `action` at all — so a missing
    `action` is treated as reviewable, while a present one is still honoured.
    """
    payload = dict(event or {})
    payload.setdefault("action", "opened")
    return should_skip_pr(payload)


def render_comment(text, part, files=None):
    """Turn a raw model answer into the comment body to post, or None to skip.

    Returns None when the review names nothing to act on, so a clean PR gets no
    comment at all. Otherwise the answer is sanitized (outer code fence removed,
    invented line anchors dropped — see `sanitize_review`) and given a short
    attribution footer naming the pass for a multi-pass review.
    """
    body = sanitize_review(text, files)
    if not review_has_action_items(body):
        return None
    index, count = part
    label = f" — part {index} of {count}" if count > 1 else ""
    return body.rstrip() + FOOTER % label


def fetch_pr_files(repo, number, token, send, max_pages=MAX_FILE_PAGES):
    """Fetch every page of the PR's changed files through `send`."""
    files = []
    for page in range(1, max_pages + 1):
        batch = send(pr_files_request(repo, number, token, page=page)) or []
        files.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return files


def run_review(
    event,
    repo,
    github_token,
    llm,
    categories,
    api_key=None,
    send=None,
    conventions="",
    exclude_globs=None,
    max_patch_chars=DEFAULT_MAX_PATCH_CHARS,
    max_batch_chars=DEFAULT_MAX_BATCH_CHARS,
):
    """Review one pull request end to end and post the findings.

    The whole runtime in one function: gate → fetch diff → filter/batch → review
    each batch → post what is worth posting. Every HTTP call goes through `send`,
    a callable taking a request spec and returning the decoded JSON response, so
    this is exercised in tests without a network (`scripts/review_pr.py` supplies
    the real urllib transport).

    Returns a small summary dict for the workflow log.
    """
    if send is None:
        raise ActionError("run_review needs a `send` transport")
    if should_skip_event(event):
        return {"skipped": True, "reviewed_files": 0, "comments_posted": 0}

    require_runner_reachable(llm)  # fail before spending a single request
    number = pull_request_from_event(event)["number"]

    files = fetch_pr_files(repo, number, github_token, send)
    passes = review_passes(
        files,
        categories,
        conventions=conventions,
        exclude_globs=exclude_globs,
        max_patch_chars=max_patch_chars,
        max_batch_chars=max_batch_chars,
    )

    posted = 0
    for review_pass in passes:
        response = send(llm_request(llm, review_pass["prompt"], api_key=api_key))
        text = extract_review_text(response, llm["api"])
        body = render_comment(text, review_pass["part"], files=review_pass["files"])
        if body is None:
            continue  # nothing to act on in this pass — post no noise
        send(comment_request(repo, number, body, github_token))
        posted += 1

    return {
        "skipped": False,
        "pull_request": number,
        "reviewed_files": sum(len(p["files"]) for p in passes),
        "passes": len(passes),
        "comments_posted": posted,
    }
