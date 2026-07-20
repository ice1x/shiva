"""Tests for the end-to-end Action run, with HTTP injected as a fake transport.

`run_review` is the whole runtime — read the event, fetch the diff, review each
batch, post the comments — with every request going through a `send` callable.
The real transport (urllib) lives in `scripts/review_pr.py`; here it is a stub,
so the full path is exercised with no network, no secrets, and no n8n.
"""

import pytest
from faker import Faker

from shiva_agent.action import ActionError, run_review

fake = Faker()
Faker.seed(20260720)

REPO = "ice1x/graphbook"
TOKEN = "ghp_token"
CATEGORIES = [{"id": "logical", "name": "Logical Review", "prompt": "Check the logic."}]
LLM = {
    "provider": "openai",
    "api": "openai",
    "endpoint": "https://api.openai.com/v1/chat/completions",
    "model": "gpt-4o-mini",
    "auth": "bearer",
    "temperature": 0,
}
FINDING = "3. **Findings**\n   - [high] Logical Review — src/app.py:2 — guard the None case"


def make_file(name="src/app.py"):
    return {
        "filename": name,
        "status": "modified",
        "patch": "@@ -1,1 +1,3 @@\n context\n+one\n+two\n",
    }


class FakeTransport:
    """Records every request and answers from a scripted queue."""

    def __init__(self, pages=None, review=FINDING):
        self.pages = list(pages if pages is not None else [[make_file()]])
        self.review = review
        self.sent = []

    def __call__(self, spec):
        self.sent.append(spec)
        url = spec["url"]
        if "/pulls/" in url and url.endswith(tuple("page=%d" % i for i in range(1, 9))):
            return self.pages.pop(0) if self.pages else []
        if "/issues/" in url:
            return {"id": len(self.sent)}
        return {"choices": [{"message": {"content": self.review}}]}

    @property
    def comments(self):
        return [s["body"]["body"] for s in self.sent if "/issues/" in s["url"]]

    @property
    def llm_calls(self):
        return [s for s in self.sent if "api.openai.com" in s["url"]]


def run(transport, event=None, **kwargs):
    event = event or {"pull_request": {"number": 42, "draft": False}}
    return run_review(
        event=event,
        repo=REPO,
        github_token=TOKEN,
        llm=LLM,
        categories=CATEGORIES,
        api_key="sk-test",
        send=transport,
        **kwargs,
    )


def test_a_pr_with_a_finding_gets_exactly_one_comment():
    transport = FakeTransport()
    result = run(transport)
    assert len(transport.comments) == 1
    assert "guard the None case" in transport.comments[0]
    assert result["comments_posted"] == 1


def test_the_posted_comment_is_sanitized():
    transport = FakeTransport(review="```markdown\n- [high] Logical Review — src/app.py:0 — x\n```")
    run(transport)
    body = transport.comments[0]
    assert not body.startswith("```")
    assert "src/app.py:0" not in body


def test_a_clean_review_posts_nothing_and_still_succeeds():
    transport = FakeTransport(review="APPROVED")
    result = run(transport)
    assert transport.comments == []
    assert result["comments_posted"] == 0
    assert result["reviewed_files"] == 1


def test_a_draft_pr_is_skipped_before_any_request():
    transport = FakeTransport()
    result = run(transport, event={"pull_request": {"number": 42, "draft": True}})
    assert result["skipped"] is True
    assert transport.sent == []


def test_a_skip_review_label_is_honoured():
    transport = FakeTransport()
    event = {
        "pull_request": {"number": 42, "draft": False, "labels": [{"name": "skip-review"}]}
    }
    result = run(transport, event=event)
    assert result["skipped"] is True
    assert transport.sent == []


def test_a_pr_with_no_reviewable_files_costs_no_llm_call():
    transport = FakeTransport(pages=[[{"filename": "poetry.lock", "patch": "+dep\n"}]])
    result = run(transport, exclude_globs=["*.lock"])
    assert transport.llm_calls == []
    assert transport.comments == []
    assert result["reviewed_files"] == 0


def test_every_page_of_a_long_file_list_is_fetched():
    full_page = [make_file("src/f%d.py" % i) for i in range(100)]
    transport = FakeTransport(pages=[full_page, [make_file("src/last.py")]])
    result = run(transport)
    file_requests = [s for s in transport.sent if "/pulls/" in s["url"]]
    assert len(file_requests) == 2
    assert result["reviewed_files"] == 101


def test_a_large_pr_posts_one_comment_per_pass():
    big = [dict(make_file("src/big%d.py" % i), patch="+x\n" * 4000) for i in range(4)]
    transport = FakeTransport(pages=[big])
    result = run(transport, max_batch_chars=10_000)
    assert len(transport.llm_calls) == len(transport.comments) > 1
    assert result["comments_posted"] == len(transport.comments)
    assert "part 1 of" in transport.comments[0]


def test_the_llm_key_rides_on_the_review_call_only():
    transport = FakeTransport()
    run(transport)
    assert transport.llm_calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    posted = [s for s in transport.sent if "/issues/" in s["url"]][0]
    assert posted["headers"]["Authorization"] == "Bearer " + TOKEN


def test_a_local_only_provider_fails_before_spending_anything():
    transport = FakeTransport()
    local = dict(LLM, provider="ollama", endpoint="http://localhost:11434/v1/chat/completions")
    with pytest.raises(ActionError):
        run_review(
            event={"pull_request": {"number": 1, "draft": False}},
            repo=REPO,
            github_token=TOKEN,
            llm=local,
            categories=CATEGORIES,
            api_key=None,
            send=transport,
        )
    assert transport.sent == []


def test_an_unusable_llm_response_does_not_post_a_comment():
    transport = FakeTransport()
    transport.review = None  # provider returned a shape we cannot read
    result = run(transport)
    assert transport.comments == []
    assert result["comments_posted"] == 0
