#!/usr/bin/env python3
"""Review a pull request from a GitHub Actions runner — the n8n-free runtime.

This is the thin I/O shell around `shiva_agent.action.run_review`: it resolves
the config (defaults + the target repo's `.shiva.yml`), reads the event payload
GitHub wrote to disk, and supplies a urllib transport. Every decision — what to
review, what to send, what to post — lives in `action.py` and is unit-tested.

Run by `action.yml`; usable by hand for a dry run:

    SHIVA_LLM_API_KEY=... GITHUB_TOKEN=... \\
    python scripts/review_pr.py --repo owner/name --pr 42 --dry-run

Secrets are read from the environment only and never logged (headers are
redacted). `--dry-run` still fetches the diff and still asks the model — it
withholds only the comment and prints it instead, so a review can be read before
it is trusted on a PR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from shiva_agent import review
from shiva_agent.action import ActionError, LLM_KEY_ENV, run_review

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "shiva.config.yml"
OVERRIDE_NAME = ".shiva.yml"
TIMEOUT_SECONDS = 300

# Header values that must never reach a log line.
SECRET_HEADERS = ("authorization", "x-api-key")


def redact(headers):
    """Copy `headers` with credential values replaced by a placeholder."""
    return {k: ("<redacted>" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}


def http_send(spec):
    """Perform one request spec and return the decoded JSON response."""
    data = json.dumps(spec["body"]).encode() if spec["body"] is not None else None
    request = urllib.request.Request(
        spec["url"], data=data, headers=spec["headers"], method=spec["method"]
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as exc:  # surface the API's own message
        detail = exc.read().decode()[:500]
        raise ActionError(
            f"{spec['method']} {spec['url'].split('?')[0]} failed: {exc.code} {detail}"
        ) from None
    except urllib.error.URLError as exc:
        raise ActionError(f"cannot reach {spec['url'].split('?')[0]}: {exc.reason}") from None
    return json.loads(payload) if payload else {}


def is_mutation(spec):
    """Whether this request changes something on GitHub (i.e. posts the comment)."""
    return spec["method"] != "GET" and "/issues/" in spec["url"]


def dry_run_send(spec):
    """Run everything except the mutation, and print the comment that was withheld.

    A dry run that stubbed out every request would prove only that the URLs are
    formatted right. This one really fetches the diff and really asks the model,
    so the review can be read before it is trusted on a PR — only the comment
    POST is withheld. Credentials are redacted from every logged header.
    """
    print(f"[dry-run] {spec['method']} {spec['url']}")
    print(f"[dry-run]   headers: {redact(spec['headers'])}")
    if not is_mutation(spec):
        return http_send(spec)
    print("[dry-run] withheld comment:\n")
    print(spec["body"]["body"])
    return {}


def load_event(path):
    """Read the webhook payload GitHub wrote for this run."""
    if not path or not Path(path).exists():
        raise ActionError(
            "no event payload: set GITHUB_EVENT_PATH (Actions does this) or pass --pr "
            "for a manual run"
        )
    return json.loads(Path(path).read_text())


def load_settings(repo, config_path=DEFAULT_CONFIG, workspace="."):
    """Resolve the effective review settings for `repo`.

    The shipped defaults, overridden by the target repository's own
    `.shiva.yml` when it has one — the same merge the workflow generator does,
    except it happens per run instead of per built workflow, so a repo edits its
    review policy without rebuilding anything.
    """
    config = yaml.safe_load(Path(config_path).read_text())
    override_path = Path(workspace) / OVERRIDE_NAME
    override = yaml.safe_load(override_path.read_text()) if override_path.exists() else None
    return {
        "categories": review.resolve_categories(config, override),
        "conventions": review.resolve_conventions(config, override),
        "exclude_globs": review.resolve_exclude(config, override),
        "llm": review.resolve_llm(config, override, repo=repo),
    }


def summarize(result, dry_run=False):
    """One honest log line: a dry run withholds comments, it does not post them."""
    if result["skipped"]:
        return "shiva: skipped (draft PR, opt-out label, or non-reviewable event)"
    verb = "withheld" if dry_run else "posted"
    return (
        "shiva: reviewed {reviewed_files} file(s) in {passes} pass(es), "
        "{verb} {comments_posted} comment(s)".format(verb=verb, **result)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pr", type=int, help="review this PR number (manual run)")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument(
        "--workspace",
        default=os.environ.get("GITHUB_WORKSPACE", "."),
        help="checkout of the target repo, searched for .shiva.yml",
    )
    parser.add_argument("--dry-run", action="store_true", help="log requests, post nothing")
    args = parser.parse_args(argv)

    try:
        if not args.repo:
            raise ActionError("--repo (or GITHUB_REPOSITORY) is required")
        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            raise ActionError("GITHUB_TOKEN is required to read the PR and post the review")

        event = (
            {"pull_request": {"number": args.pr, "draft": False}}
            if args.pr
            else load_event(os.environ.get("GITHUB_EVENT_PATH"))
        )
        settings = load_settings(args.repo, args.config, args.workspace)
        llm = settings["llm"]
        print(f"shiva: reviewing {args.repo} with {llm['provider']}/{llm['model']}")

        result = run_review(
            event=event,
            repo=args.repo,
            github_token=token,
            llm=llm,
            categories=settings["categories"],
            api_key=os.environ.get(LLM_KEY_ENV) or None,
            send=dry_run_send if args.dry_run else http_send,
            conventions=settings["conventions"],
            exclude_globs=settings["exclude_globs"],
        )
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(summarize(result, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
