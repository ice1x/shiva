import pytest
from faker import Faker

from shiva_agent.feedback import (
    ROOT_CAUSES,
    SCHEMA_VERSION,
    VERDICTS,
    build_feedback_record,
    parse_jsonl,
    summarize,
    to_jsonl_line,
    validate_feedback_record,
)

fake = Faker()
TS = "2026-07-06T21:40:00Z"


def a_finding(**over):
    base = {
        "severity": "medium",
        "category": "structural",
        "file": "src/x.ts",
        "lines": "58",
        "claim": "isBookmark misleading; rename",
        "suggested_fix": "rename",
    }
    base.update(over)
    return base


def a_record(**over):
    kw = dict(
        repo="ice1x/graphbook", pr=90, verdict="reject",
        reason="positive presence check, not validation",
        finding=a_finding(), ts=TS, root_cause="misread_code",
        llm={"provider": "openai", "model": "gpt-4o"},
    )
    kw.update(over)
    return build_feedback_record(**kw)


# --- build / validate ------------------------------------------------------


def test_record_has_schema_and_core_fields():
    r = a_record()
    assert r["schema_version"] == SCHEMA_VERSION
    assert r["repo"] == "ice1x/graphbook" and r["pr"] == 90
    assert r["verdict"] == "reject" and r["root_cause"] == "misread_code"
    assert r["finding"]["claim"] == "isBookmark misleading; rename"
    assert r["ts"] == TS


@pytest.mark.parametrize("bad", ["", "no-slash"])
def test_rejects_bad_repo(bad):
    with pytest.raises(ValueError):
        a_record(repo=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_rejects_bad_pr(bad):
    with pytest.raises(ValueError):
        a_record(pr=bad)


def test_rejects_unknown_verdict():
    with pytest.raises(ValueError):
        a_record(verdict="maybe")


def test_rejects_unknown_root_cause():
    with pytest.raises(ValueError):
        a_record(root_cause="dunno")


def test_requires_reason_and_claim():
    with pytest.raises(ValueError):
        a_record(reason="  ")
    with pytest.raises(ValueError):
        a_record(finding=a_finding(claim=""))


def test_all_verdicts_and_root_causes_accepted():
    for v in VERDICTS:
        a_record(verdict=v)
    for rc in ROOT_CAUSES:
        a_record(root_cause=rc)


def test_validate_record_roundtrips():
    validate_feedback_record(a_record())  # no raise


# --- jsonl -----------------------------------------------------------------


def test_jsonl_roundtrip():
    r = a_record()
    line = to_jsonl_line(r)
    assert line.endswith("\n")
    (back,) = parse_jsonl(line)
    assert back == r


def test_parse_skips_blank_lines():
    text = to_jsonl_line(a_record()) + "\n  \n" + to_jsonl_line(a_record(pr=91))
    assert len(parse_jsonl(text)) == 2


# --- summarize (the L0 signal) --------------------------------------------


def test_summary_counts_verdicts_causes_and_categories():
    records = [
        a_record(verdict="reject", root_cause="misread_code", finding=a_finding(category="structural")),
        a_record(verdict="reject", root_cause="nitpick", finding=a_finding(category="performance")),
        a_record(verdict="accept", root_cause="correct", finding=a_finding(category="security")),
    ]
    s = summarize(records)
    assert s["total"] == 3
    assert s["verdicts"] == {"reject": 2, "accept": 1}
    assert s["root_causes"] == {"misread_code": 1, "nitpick": 1, "correct": 1}
    assert s["rejected_by_category"] == {"structural": 1, "performance": 1}
