# Review feedback log

`review_feedback.jsonl` is an append-only, committed log of **human verdicts on
bot review findings** — a labelled dataset of `bot finding -> verdict + reason`
used to improve the reviewer over time.

One JSON object per line (schema + validation in
[`src/shiva_agent/feedback.py`](../src/shiva_agent/feedback.py); write/read via
[`scripts/feedback.py`](../scripts/feedback.py)). One record per **finding**:

```json
{
  "schema_version": 1,
  "ts": "2026-07-06T21:40:00Z",
  "repo": "ice1x/graphbook",
  "pr": 90,
  "head_sha": null,
  "comment_url": "https://github.com/ice1x/graphbook/pull/90#issuecomment-...",
  "llm": {"provider": "openai", "model": "gpt-4o"},
  "finding": {
    "severity": "medium", "category": "structural",
    "file": "src/bookmarks.ts", "lines": "58",
    "claim": "isBookmark misleading; rename to isValidBookmark",
    "suggested_fix": "rename"
  },
  "verdict": "reject",
  "root_cause": "misread_code",
  "reason": "positive presence check, not validation; validity is isCapturableUrl",
  "reviewer": "ice1x"
}
```

- `verdict`: `accept` | `reject` | `fixed` | `wontfix`
- `root_cause` (the learning signal): `correct` | `misread_code` |
  `hallucinated_line` | `nitpick` | `conflicts_design` | `other`

## How this feeds review improvement

- **L0 (now):** `python scripts/feedback.py summary` aggregates the log by
  verdict / root cause / category. Recurring rejections become global prompt
  rules (see `build_review_prompt`'s "Review discipline" section) and per-repo
  `conventions` in that repo's `.shiva.yml`.
- **L1:** a verify/self-critique pass that checks each finding against the diff
  before posting.
- **L2:** load the records into the drevo knowledge graph for
  retrieval-augmented review (inject relevant past rejections per PR).
