#!/usr/bin/env python3
"""Record and summarize human verdicts on bot review findings.

Appends one JSON object per finding to data/review_feedback.jsonl (append-only,
committed). Use the accumulated log to improve the reviewer: `summary` shows the
recurring failure modes to distil into prompt rules / per-repo `conventions`.

    # log a rejected finding from a review comment
    python scripts/feedback.py add \
        --repo ice1x/graphbook --pr 90 \
        --file src/bookmarks.ts --lines 58 --severity medium --category structural \
        --claim "isBookmark misleading; rename to isValidBookmark" \
        --verdict reject --root-cause misread_code \
        --reason "positive presence check, not validation" \
        --llm-provider openai --llm-model gpt-4o

    # see what the bot systematically gets wrong
    python scripts/feedback.py summary
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from shiva_agent.feedback import (
    ROOT_CAUSES,
    VERDICTS,
    build_feedback_record,
    parse_jsonl,
    summarize,
    to_jsonl_line,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "data" / "review_feedback.jsonl"


def cmd_add(args: argparse.Namespace) -> int:
    llm = None
    if args.llm_provider or args.llm_model:
        llm = {"provider": args.llm_provider, "model": args.llm_model}
    record = build_feedback_record(
        repo=args.repo,
        pr=args.pr,
        verdict=args.verdict,
        reason=args.reason,
        finding={
            "severity": args.severity,
            "category": args.category,
            "file": args.file,
            "lines": args.lines,
            "claim": args.claim,
            "suggested_fix": args.suggested_fix,
        },
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        llm=llm,
        head_sha=args.head_sha,
        comment_url=args.comment_url,
        root_cause=args.root_cause,
        reviewer=args.reviewer,
    )
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(to_jsonl_line(record))
    print(f"appended feedback for {args.repo}#{args.pr} ({args.verdict}) -> {LOG_PATH}")
    return 0


def cmd_summary(_args: argparse.Namespace) -> int:
    if not LOG_PATH.exists():
        print(f"no feedback yet ({LOG_PATH} does not exist)")
        return 0
    records = parse_jsonl(LOG_PATH.read_text(encoding="utf-8"))
    print(json.dumps(summarize(records), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="append one finding + verdict to the log")
    add.add_argument("--repo", required=True, help="owner/name")
    add.add_argument("--pr", type=int, required=True)
    add.add_argument("--claim", required=True, help="the bot's finding text")
    add.add_argument("--verdict", required=True, choices=VERDICTS)
    add.add_argument("--reason", required=True, help="why you accepted/rejected it")
    add.add_argument("--root-cause", dest="root_cause", choices=ROOT_CAUSES, default=None)
    add.add_argument("--severity", default=None)
    add.add_argument("--category", default=None)
    add.add_argument("--file", default=None)
    add.add_argument("--lines", default=None)
    add.add_argument("--suggested-fix", dest="suggested_fix", default=None)
    add.add_argument("--llm-provider", dest="llm_provider", default=None)
    add.add_argument("--llm-model", dest="llm_model", default=None)
    add.add_argument("--comment-url", dest="comment_url", default=None)
    add.add_argument("--head-sha", dest="head_sha", default=None)
    add.add_argument("--reviewer", default=None)

    sub.add_parser("summary", help="aggregate the log into the L0 signal")

    args = parser.parse_args(argv)
    return {"add": cmd_add, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
