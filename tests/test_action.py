"""Tests for the GitHub Action runtime (the n8n-free execution path).

`action.py` holds the pure decisions — which PR to review, what HTTP requests to
make, what to post — so the CLI in `scripts/review_pr.py` is a thin I/O shell
over logic that is unit-tested here without a network.
"""

import pytest
from faker import Faker

from shiva_agent.action import (
    ActionError,
    comment_request,
    llm_request,
    pr_files_request,
    pull_request_from_event,
    render_comment,
    require_runner_reachable,
    review_passes,
)
from shiva_agent.review import NO_FINDINGS_SENTINEL

fake = Faker()
Faker.seed(20260720)

REPO = "ice1x/graphbook"
TOKEN = "ghp_" + fake.lexify("?" * 20)


# --- reading the event ------------------------------------------------------


def test_the_pull_request_number_comes_from_the_event_payload():
    pr = pull_request_from_event({"pull_request": {"number": 42, "draft": False}})
    assert pr["number"] == 42


def test_an_event_without_a_pull_request_is_rejected():
    with pytest.raises(ActionError) as exc:
        pull_request_from_event({"issue": {"number": 42}})
    assert "pull_request" in str(exc.value)


# --- GitHub requests --------------------------------------------------------


def test_the_files_request_targets_the_pulls_files_endpoint():
    req = pr_files_request(REPO, 42, TOKEN, page=2)
    assert req["url"] == "https://api.github.com/repos/ice1x/graphbook/pulls/42/files?per_page=100&page=2"
    assert req["headers"]["Authorization"] == "Bearer " + TOKEN
    assert req["headers"]["Accept"] == "application/vnd.github+json"


def test_the_files_request_defaults_to_the_first_page():
    assert pr_files_request(REPO, 1, TOKEN)["url"].endswith("page=1")


def test_the_comment_request_posts_to_the_issue_comments_endpoint():
    req = comment_request(REPO, 42, "the review", TOKEN)
    assert req["url"] == "https://api.github.com/repos/ice1x/graphbook/issues/42/comments"
    assert req["body"] == {"body": "the review"}
    assert req["headers"]["Authorization"] == "Bearer " + TOKEN


# --- LLM requests -----------------------------------------------------------

OPENAI_LLM = {
    "provider": "openai",
    "api": "openai",
    "endpoint": "https://api.openai.com/v1/chat/completions",
    "model": "gpt-4o-mini",
    "auth": "bearer",
    "temperature": 0,
}
ANTHROPIC_LLM = {
    "provider": "anthropic",
    "api": "anthropic",
    "endpoint": "https://api.anthropic.com/v1/messages",
    "model": "claude-opus-4-8",
    "auth": "x-api-key",
    "temperature": 0,
}


def test_an_openai_request_carries_the_prompt_and_a_bearer_key():
    req = llm_request(OPENAI_LLM, "review this", api_key="sk-secret")
    assert req["url"] == OPENAI_LLM["endpoint"]
    assert req["headers"]["Authorization"] == "Bearer sk-secret"
    assert req["body"]["messages"][0]["content"] == "review this"
    assert req["body"]["model"] == "gpt-4o-mini"


def test_an_anthropic_request_uses_the_x_api_key_header_and_version():
    req = llm_request(ANTHROPIC_LLM, "review this", api_key="sk-ant")
    assert req["headers"]["x-api-key"] == "sk-ant"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["body"]["messages"][0]["content"] == "review this"


def test_a_keyless_provider_sends_no_auth_header():
    keyless = dict(OPENAI_LLM, provider="ollama", auth="none")
    assert "Authorization" not in llm_request(keyless, "p", api_key=None)["headers"]


def test_a_provider_that_needs_a_key_fails_loudly_without_one():
    with pytest.raises(ActionError) as exc:
        llm_request(OPENAI_LLM, "p", api_key=None)
    assert "openai" in str(exc.value)
    assert "SHIVA_LLM_API_KEY" in str(exc.value)


def test_the_prompt_sentinel_never_survives_into_the_request():
    body = llm_request(OPENAI_LLM, fake.paragraph(), api_key="k")["body"]
    assert "__SHIVA_PROMPT__" not in str(body)


# --- a runner cannot reach a laptop-local model -----------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://host.docker.internal:11434/v1/chat/completions",
        "http://localhost:11434/v1/chat/completions",
        "http://127.0.0.1:1234/v1/chat/completions",
    ],
)
def test_a_local_endpoint_is_rejected_with_an_actionable_message(endpoint):
    with pytest.raises(ActionError) as exc:
        require_runner_reachable(dict(OPENAI_LLM, provider="ollama", endpoint=endpoint))
    message = str(exc.value)
    assert "ollama" in message
    assert "runner" in message.lower()


def test_a_hosted_endpoint_is_accepted():
    assert require_runner_reachable(OPENAI_LLM) is None


# --- planning the review passes ---------------------------------------------

CATEGORIES = [{"id": "logical", "name": "Logical Review", "prompt": "Check the logic."}]


def make_file(name, added=3):
    patch = "@@ -1,1 +1,%d @@\n context\n" % (added + 1)
    patch += "".join("+%s\n" % fake.word() for _ in range(added))
    return {"filename": name, "status": "modified", "patch": patch}


def test_every_pass_carries_a_prompt_and_the_files_it_reviewed():
    files = [make_file("a.py"), make_file("b.py")]
    passes = review_passes(files, CATEGORIES)
    assert len(passes) == 1
    assert "Check the logic." in passes[0]["prompt"]
    assert passes[0]["files"] == files


def test_a_large_pr_is_split_into_several_passes():
    files = [dict(make_file("big%d.py" % i), patch="+x\n" * 4000) for i in range(4)]
    passes = review_passes(files, CATEGORIES, max_batch_chars=10_000)
    assert len(passes) > 1
    assert sum(len(p["files"]) for p in passes) == 4
    assert [p["part"] for p in passes] == [(i + 1, len(passes)) for i in range(len(passes))]


def test_excluded_and_unreviewable_files_never_reach_a_pass():
    files = [
        make_file("src/app.py"),
        {"filename": "poetry.lock", "status": "modified", "patch": "+deps\n"},
        {"filename": "img.png", "status": "modified"},  # binary: no patch
        {"filename": "gone.py", "status": "removed", "patch": "-x\n"},
    ]
    passes = review_passes(files, CATEGORIES, exclude_globs=["*.lock"])
    assert [f["filename"] for p in passes for f in p["files"]] == ["src/app.py"]


def test_a_pr_with_nothing_reviewable_yields_no_passes():
    files = [{"filename": "poetry.lock", "status": "modified", "patch": "+deps\n"}]
    assert review_passes(files, CATEGORIES, exclude_globs=["*.lock"]) == []


def test_the_conventions_reach_the_prompt():
    passes = review_passes([make_file("a.py")], CATEGORIES, conventions="Never log secrets.")
    assert "Never log secrets." in passes[0]["prompt"]


# --- rendering the comment --------------------------------------------------


def test_a_review_is_posted_with_an_attribution_footer():
    body = render_comment("3. **Findings**\n   - [high] Logical Review — a.py:2 — fix it", (1, 1))
    assert body.startswith("3. **Findings**")
    assert "Shiva" in body


def test_a_multi_pass_review_labels_which_part_it_is():
    body = render_comment("- [low] Logical Review — a.py:2 — nit", (2, 3))
    assert "part 2 of 3" in body


def test_a_single_pass_review_is_not_labelled_with_a_part():
    assert "part" not in render_comment("- [low] x — a.py:2 — nit", (1, 1)).lower()


def test_an_invented_line_number_is_stripped_from_the_posted_body():
    files = [make_file("a.py")]
    raw = "- [medium] Logical Review — a.py:0 — something"
    assert "a.py:0" not in render_comment(raw, (1, 1), files=files)


def test_a_fenced_answer_is_unwrapped_before_posting():
    body = render_comment("```markdown\n- [low] Logical Review — a.py:2 — nit\n```", (1, 1))
    assert not body.startswith("```")


@pytest.mark.parametrize("text", ["", None, NO_FINDINGS_SENTINEL, "  %s  " % NO_FINDINGS_SENTINEL])
def test_a_review_with_nothing_to_act_on_is_not_rendered(text):
    assert render_comment(text, (1, 1)) is None
