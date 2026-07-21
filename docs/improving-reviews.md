# Improving the reviewer (leaving feedback)

Reviews are only as good as the prompt and the per-repo conventions behind them.
When a finding is wrong or a nitpick, record your verdict so it can be turned
into a rule — an append-only log at
[`data/review_feedback.jsonl`](../data/review_feedback.jsonl) (schema + how it feeds
review improvement in [`data/README.md`](../data/README.md)):

```bash
# log a verdict on one finding
.venv/bin/python scripts/feedback.py add \
    --repo ice1x/graphbook --pr 90 \
    --file src/bookmarks.ts --lines 58 --severity medium --category structural \
    --claim "isBookmark misleading; rename to isValidBookmark" \
    --verdict reject --root-cause misread_code \
    --reason "positive presence check, not validation"

# see what the bot systematically gets wrong (verdicts / root causes / categories)
.venv/bin/python scripts/feedback.py summary
```

Act on the signal in two places: recurring global mistakes → the "Review
discipline" rules in [`build_review_prompt`](../src/shiva_agent/review.py);
repo-specific facts → that repo's `.shiva.yml` `conventions`
(see [Per-repo configuration](#per-repo-configuration)). A verify pass and
knowledge-graph retrieval are the later, larger steps described in
[`data/README.md`](../data/README.md).
