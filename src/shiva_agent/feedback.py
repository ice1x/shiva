"""Review-feedback records: capture a human verdict on each bot finding.

Side-effect-free schema + validation for the JSONL feedback log
(``data/review_feedback.jsonl``). The point of the log is a labelled dataset of
``bot finding -> human verdict + reason`` that later feeds review improvement:
first by distilling recurring rejections into prompt rules / per-repo
``conventions`` (L0), then a verify pass (L1), then retrieval (L2).

One record per finding, one JSON object per line. The I/O (append / read the
file, stamp the timestamp) lives in ``scripts/feedback.py``; everything here is
pure so it is unit-tested without touching disk.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1

# What the human decided about a finding.
VERDICTS = ("accept", "reject", "fixed", "wontfix")

# Why the finding was accepted/rejected — the field that turns the log into a
# signal ("what does the bot systematically get wrong").
ROOT_CAUSES = (
    "correct",           # the finding was right
    "misread_code",      # the bot misread what the code does
    "hallucinated_line", # wrong / invented line numbers or location
    "nitpick",           # style / micro-optimization not worth raising
    "conflicts_design",  # technically true but fights an intentional decision
    "other",
)

FINDING_FIELDS = ("severity", "category", "file", "lines", "claim", "suggested_fix")


def build_feedback_record(
    *,
    repo: str,
    pr: int,
    verdict: str,
    reason: str,
    finding: Mapping[str, Any],
    ts: str,
    llm: Mapping[str, str] | None = None,
    head_sha: str | None = None,
    comment_url: str | None = None,
    root_cause: str | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Assemble one validated feedback record (a dict ready for JSONL).

    ``ts`` is passed in (ISO-8601) rather than read here, so the function stays
    pure and deterministic; the CLI stamps the current time. Raises ValueError on
    a bad enum / missing required field.
    """
    if not repo or "/" not in repo:
        raise ValueError("repo must be an 'owner/name' string")
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError("pr must be a positive int")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if root_cause is not None and root_cause not in ROOT_CAUSES:
        raise ValueError(f"root_cause must be one of {ROOT_CAUSES}, got {root_cause!r}")
    if not reason or not reason.strip():
        raise ValueError("reason must be a non-empty string")

    finding_out = {k: finding.get(k) for k in FINDING_FIELDS}
    if not finding_out.get("claim"):
        raise ValueError("finding.claim is required")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts,
        "repo": repo,
        "pr": pr,
        "head_sha": head_sha,
        "comment_url": comment_url,
        "llm": dict(llm) if llm else None,
        "finding": finding_out,
        "verdict": verdict,
        "root_cause": root_cause,
        "reason": reason.strip(),
        "reviewer": reviewer,
    }
    return record


def validate_feedback_record(record: Mapping[str, Any]) -> None:
    """Raise ValueError unless ``record`` is a well-formed feedback record."""
    build_feedback_record(
        repo=record.get("repo", ""),
        pr=record.get("pr", 0) if isinstance(record.get("pr"), int) else 0,
        verdict=record.get("verdict", ""),
        reason=record.get("reason", ""),
        finding=record.get("finding") or {},
        ts=record.get("ts", ""),
        llm=record.get("llm"),
        head_sha=record.get("head_sha"),
        comment_url=record.get("comment_url"),
        root_cause=record.get("root_cause"),
        reviewer=record.get("reviewer"),
    )


def to_jsonl_line(record: Mapping[str, Any]) -> str:
    """Serialize one record to a single JSONL line (with trailing newline)."""
    return json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse JSONL text into records, skipping blank lines."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def summarize(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate feedback into the L0 signal: how often, and why, findings are
    rejected — by verdict, root cause, and category. This is what you read to
    decide which prompt rules / per-repo conventions to add."""
    records = list(records)
    verdicts = Counter(r.get("verdict") for r in records)
    root_causes = Counter(r.get("root_cause") for r in records if r.get("root_cause"))
    rejected = [r for r in records if r.get("verdict") in ("reject", "wontfix")]
    rejected_by_category = Counter(
        (r.get("finding") or {}).get("category") for r in rejected
    )
    return {
        "total": len(records),
        "verdicts": dict(verdicts),
        "root_causes": dict(root_causes),
        "rejected_by_category": dict(rejected_by_category),
    }
