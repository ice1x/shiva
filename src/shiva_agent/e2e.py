"""Pure helpers for the end-to-end (task ``00008``) automation.

This module holds only side-effect-free logic so it can be unit-tested without a
running n8n, a tunnel, or GitHub. The I/O orchestration (shelling out to the
``n8n`` CLI and ``gh``, polling, secret handling) lives in ``scripts/e2e.py``.

No secret ever originates here: :func:`build_header_auth_credential` receives an
already-resolved token from the caller (env / ``gh auth token``) and only shapes
it into the JSON n8n expects. Nothing in this file is written to a tracked file.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# Must match the Webhook node's ``path`` in workflows/pr_review.json and the two
# GitHub HTTP Request nodes that carry the PAT credential.
WEBHOOK_PATH = "pr-review"
GITHUB_CREDENTIAL_NODES = ("Fetch PR Files", "Post PR Comment")

# n8n credential type + the header the workflow authenticates with.
HTTP_HEADER_AUTH_TYPE = "httpHeaderAuth"
AUTH_HEADER_NAME = "Authorization"

# Default display name for the imported GitHub PAT credential.
CREDENTIAL_NAME = "shiva-github-pat"

# GitHub only delivers these PR actions to a review-worthy webhook.
REVIEWABLE_ACTIONS = ("opened", "ready_for_review", "reopened", "synchronize")


def webhook_url(base_url: str) -> str:
    """Full webhook endpoint GitHub should POST to, from a tunnel/base URL."""
    return f"{base_url.rstrip('/')}/webhook/{WEBHOOK_PATH}"


def build_synthetic_pr_event(
    full_name: str,
    number: int,
    *,
    action: str = "opened",
    draft: bool = False,
    labels: Sequence[str] = (),
) -> dict[str, Any]:
    """A minimal ``pull_request`` webhook body the workflow understands.

    Mirrors the fields the generated nodes read: ``body.action``,
    ``body.repository.full_name`` and ``body.pull_request.{number,draft,labels}``.
    Used by the local smoke test to exercise the webhook + skip gate without a
    real GitHub delivery.
    """
    return {
        "body": {
            "action": action,
            "repository": {"full_name": full_name},
            "pull_request": {
                "number": number,
                "draft": draft,
                "labels": [{"name": name} for name in labels],
            },
        }
    }


def build_header_auth_credential(
    token: str, *, name: str = "shiva-github-pat", credential_id: str | None = None
) -> dict[str, Any]:
    """Shape a GitHub PAT into an n8n ``import:credentials`` record.

    The token is placed in the ``Authorization: Bearer <token>`` header value.
    ``token`` must be non-empty — an empty token is a caller bug, not a valid
    "anonymous" credential, so we fail loudly rather than import a broken secret.
    """
    if not token or not token.strip():
        raise ValueError("refusing to build a credential from an empty token")
    record: dict[str, Any] = {
        "name": name,
        "type": HTTP_HEADER_AUTH_TYPE,
        "data": {"name": AUTH_HEADER_NAME, "value": f"Bearer {token}"},
    }
    if credential_id is not None:
        record["id"] = credential_id
    return record


def attach_credential_to_workflow(
    workflow: Mapping[str, Any],
    credential_id: str,
    credential_name: str,
    *,
    node_names: Iterable[str] = GITHUB_CREDENTIAL_NODES,
) -> dict[str, Any]:
    """Return a copy of ``workflow`` with the header-auth credential wired in.

    Only the named GitHub HTTP nodes get the reference; every other node is left
    untouched. The input is not mutated. Node names not present in the workflow
    are ignored (the caller validates coverage separately).
    """
    targets = set(node_names)
    ref = {HTTP_HEADER_AUTH_TYPE: {"id": credential_id, "name": credential_name}}
    new_nodes = []
    for node in workflow.get("nodes", []):
        node = dict(node)
        if node.get("name") in targets:
            node["credentials"] = {**node.get("credentials", {}), **ref}
        new_nodes.append(node)
    return {**workflow, "nodes": new_nodes}


def missing_credential_nodes(
    workflow: Mapping[str, Any], *, node_names: Iterable[str] = GITHUB_CREDENTIAL_NODES
) -> list[str]:
    """Names in ``node_names`` that lack a header-auth credential reference.

    Empty list ⇒ every GitHub node is wired up; used to fail fast before
    activating a workflow that would 401 against GitHub at runtime.
    """
    present = {n.get("name"): n for n in workflow.get("nodes", [])}
    missing = []
    for name in node_names:
        node = present.get(name)
        if node is None or HTTP_HEADER_AUTH_TYPE not in (node.get("credentials") or {}):
            missing.append(name)
    return missing


def llm_node_needs_credential(
    workflow: Mapping[str, Any], *, node_name: str = "LLM Review"
) -> bool:
    """True when the review node authenticates with an n8n credential.

    A hosted provider (OpenAI/DeepSeek/Anthropic/…) sets the LLM Review node to
    ``genericCredentialType`` and needs an API-key credential wired in; a local
    keyless provider (Ollama) does not. Lets the E2E driver decide whether to
    import a second credential.
    """
    for node in workflow.get("nodes", []):
        if node.get("name") == node_name:
            return node.get("parameters", {}).get("authentication") == "genericCredentialType"
    return False


def mapped_providers(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every distinct LLM provider the config references, hosted or local.

    Scans the default ``llm`` and every ``llm_by_repo`` entry and resolves each,
    merging by provider. Each result is ``{provider, labels, auth, endpoint,
    needs_key}`` where a label is a repo ``owner/name`` or ``"<default>"`` for the
    base ``llm``, and ``needs_key`` is ``auth != "none"`` (hosted needs an API
    key; a local provider needs a reachable server instead). Lets the deployment
    check validate *all* mapped providers, not just the hosted ones.
    """
    from shiva_agent import review

    seen: dict[str, dict[str, Any]] = {}
    entries: list[tuple[str, str | None]] = [("<default>", None)]
    entries += [(repo, repo) for repo in (config.get("llm_by_repo") or {})]
    for label, repo in entries:
        llm = review.resolve_llm(config, repo=repo)
        info = seen.setdefault(
            llm["provider"],
            {
                "provider": llm["provider"],
                "labels": [],
                "auth": llm["auth"],
                "endpoint": llm["endpoint"],
                "needs_key": llm["auth"] != "none",
            },
        )
        info["labels"].append(label)
    return list(seen.values())


def hosted_providers_in_config(config: Mapping[str, Any]) -> dict[str, list[str]]:
    """Key-requiring providers only, as ``{provider: [labels]}`` (see
    :func:`mapped_providers`)."""
    return {p["provider"]: p["labels"] for p in mapped_providers(config) if p["needs_key"]}


def build_webhook_config(
    payload_url: str, *, secret: str | None = None
) -> dict[str, Any]:
    """Config for ``POST /repos/{owner}/{repo}/hooks`` (a PR-only webhook)."""
    config: dict[str, Any] = {"url": payload_url, "content_type": "json"}
    if secret:
        config["secret"] = secret
    return {
        "name": "web",
        "active": True,
        "events": ["pull_request"],
        "config": config,
    }


def find_existing_hook_id(
    hooks: Iterable[Mapping[str, Any]], payload_url: str
) -> int | None:
    """Id of an existing repo webhook pointing at ``payload_url`` (idempotency)."""
    for hook in hooks:
        if (hook.get("config") or {}).get("url") == payload_url:
            hook_id = hook.get("id")
            return int(hook_id) if hook_id is not None else None
    return None


def flawed_python_sample() -> str:
    """Intentionally flawed .py content for the throwaway test PR.

    Packs several findings the review should catch: a hardcoded secret, a
    SQL-injection f-string, a bare ``except`` that swallows errors, and a mutable
    default argument.
    """
    return (
        "import sqlite3\n\n"
        "PASSWORD = \"hunter2\"  # hardcoded secret\n\n\n"
        "def get_user(db, user_id, cache={}):  # mutable default arg\n"
        "    query = f\"SELECT * FROM users WHERE id = {user_id}\"  # SQL injection\n"
        "    try:\n"
        "        return db.execute(query).fetchone()\n"
        "    except:  # bare except swallows everything\n"
        "        return None\n"
    )


def redact(text: str, *secrets: str) -> str:
    """Replace every non-empty secret in ``text`` with a fixed placeholder.

    Used so ``--dry-run`` output can show credential/command shapes without ever
    printing the token. A secret is also redacted inside a ``Bearer <token>``
    value because that substring still contains it verbatim.
    """
    placeholder = "***REDACTED***"
    for secret in secrets:
        if secret:
            text = text.replace(secret, placeholder)
    return text


def describe_live_plan(
    repo: str, payload_url: str, branch: str, *, keep: bool = False
) -> list[str]:
    """Ordered, human-readable actions a ``live`` run *would* take.

    Pure and side-effect-free so ``--dry-run`` can print it and tests can assert
    it. Contains no secrets.
    """
    steps = [
        f"import GitHub PAT credential '{CREDENTIAL_NAME}' into n8n (token from env/gh, encrypted at rest)",
        f"wire credential into nodes {list(GITHUB_CREDENTIAL_NODES)} and activate workflow",
        f"ensure PR-only webhook on {repo} → {payload_url} (idempotent)",
        f"create branch '{branch}' and commit shiva_e2e_sample.py (intentionally flawed)",
        f"open a throwaway PR on {repo}",
        "poll the PR's comments until the review appears or timeout",
    ]
    if keep:
        steps.append("leave the PR open and the branch in place (--keep)")
    else:
        steps.append("close the PR and delete the branch")
    return steps


def find_review_comment(
    comments: Iterable[Mapping[str, Any]],
    *,
    author: str | None = None,
    since: str | None = None,
) -> dict[str, Any] | None:
    """First issue comment that looks like the posted review.

    On a throwaway PR the review is the only comment, so a match is any comment
    optionally filtered by ``author`` (the PAT's login) and ``since`` (ISO
    timestamp of PR creation, to ignore anything predating the run). Returns the
    comment dict or ``None``.
    """
    for comment in comments:
        body = (comment.get("body") or "").strip()
        if not body:
            continue
        if author is not None and (comment.get("user") or {}).get("login") != author:
            continue
        if since is not None and comment.get("created_at", "") < since:
            continue
        return dict(comment)
    return None
