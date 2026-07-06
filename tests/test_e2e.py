import json
from pathlib import Path

import pytest
from faker import Faker

from shiva_agent.e2e import (
    AUTH_HEADER_NAME,
    GITHUB_CREDENTIAL_NODES,
    HTTP_HEADER_AUTH_TYPE,
    WEBHOOK_PATH,
    attach_credential_to_workflow,
    build_header_auth_credential,
    build_synthetic_pr_event,
    build_webhook_config,
    describe_live_plan,
    find_existing_hook_id,
    find_review_comment,
    flawed_python_sample,
    llm_node_needs_credential,
    missing_credential_nodes,
    redact,
    webhook_url,
)
from shiva_agent.review import should_skip_pr

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / "workflows" / "pr_review.json"

fake = Faker()


@pytest.fixture(scope="module")
def workflow():
    return json.loads(WORKFLOW_PATH.read_text())


# --- webhook_url -----------------------------------------------------------


@pytest.mark.parametrize("base", ["https://x.trycloudflare.com", "https://x.trycloudflare.com/"])
def test_webhook_url_joins_without_double_slash(base):
    assert webhook_url(base) == f"https://x.trycloudflare.com/webhook/{WEBHOOK_PATH}"


# --- build_synthetic_pr_event ---------------------------------------------


def test_synthetic_event_matches_fields_the_workflow_reads():
    full_name = f"{fake.user_name()}/{fake.slug()}"
    number = fake.random_int(1, 9999)
    event = build_synthetic_pr_event(full_name, number)
    body = event["body"]
    assert body["repository"]["full_name"] == full_name
    assert body["pull_request"]["number"] == number
    assert body["action"] == "opened"
    assert body["pull_request"]["draft"] is False
    assert body["pull_request"]["labels"] == []


def test_synthetic_default_event_is_not_skipped():
    event = build_synthetic_pr_event(f"{fake.user_name()}/repo", 1)
    assert should_skip_pr(event["body"]) is False


def test_synthetic_draft_event_is_skipped():
    event = build_synthetic_pr_event("o/r", 1, draft=True)
    assert should_skip_pr(event["body"]) is True


def test_synthetic_skip_label_event_is_skipped():
    event = build_synthetic_pr_event("o/r", 1, labels=["skip-review"])
    assert should_skip_pr(event["body"]) is True


def test_synthetic_non_reviewable_action_is_skipped():
    event = build_synthetic_pr_event("o/r", 1, action="closed")
    assert should_skip_pr(event["body"]) is True


# --- build_header_auth_credential -----------------------------------------


def test_credential_puts_bearer_token_in_authorization_header():
    token = fake.sha256()
    cred = build_header_auth_credential(token, name="my-cred")
    assert cred["type"] == HTTP_HEADER_AUTH_TYPE
    assert cred["name"] == "my-cred"
    assert cred["data"] == {"name": AUTH_HEADER_NAME, "value": f"Bearer {token}"}
    assert "id" not in cred


def test_credential_includes_id_when_given():
    cred = build_header_auth_credential(fake.sha256(), credential_id="abc123")
    assert cred["id"] == "abc123"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_credential_rejects_empty_token(bad):
    with pytest.raises(ValueError):
        build_header_auth_credential(bad)


def test_credential_never_leaks_token_into_type_or_name():
    token = fake.sha256()
    cred = build_header_auth_credential(token)
    assert token not in cred["type"]
    assert token not in cred["name"]


# --- attach_credential_to_workflow / missing_credential_nodes -------------


def test_attach_wires_only_github_nodes(workflow):
    wired = attach_credential_to_workflow(workflow, "cid", "cname")
    by_name = {n["name"]: n for n in wired["nodes"]}
    for name in GITHUB_CREDENTIAL_NODES:
        ref = by_name[name]["credentials"][HTTP_HEADER_AUTH_TYPE]
        assert ref == {"id": "cid", "name": "cname"}
    # A non-GitHub node keeps no injected credential.
    assert HTTP_HEADER_AUTH_TYPE not in (by_name["LLM Review"].get("credentials") or {})


def test_attach_does_not_mutate_input(workflow):
    before = json.dumps(workflow, sort_keys=True)
    attach_credential_to_workflow(workflow, "cid", "cname")
    assert json.dumps(workflow, sort_keys=True) == before


def test_missing_before_and_after_attach(workflow):
    assert set(missing_credential_nodes(workflow)) == set(GITHUB_CREDENTIAL_NODES)
    wired = attach_credential_to_workflow(workflow, "cid", "cname")
    assert missing_credential_nodes(wired) == []


# --- llm_node_needs_credential --------------------------------------------


def test_default_workflow_llm_is_keyless(workflow):
    # The committed default targets local Ollama — no LLM credential needed.
    assert llm_node_needs_credential(workflow) is False


def test_hosted_llm_node_needs_credential(workflow):
    hosted = {
        "nodes": [
            {"name": "LLM Review", "parameters": {"authentication": "genericCredentialType"}},
        ]
    }
    assert llm_node_needs_credential(hosted) is True


def test_llm_needs_credential_false_when_node_absent():
    assert llm_node_needs_credential({"nodes": []}) is False


# --- build_webhook_config / find_existing_hook_id -------------------------


def test_webhook_config_is_pr_only_json():
    url = webhook_url(f"https://{fake.domain_name()}")
    cfg = build_webhook_config(url)
    assert cfg["events"] == ["pull_request"]
    assert cfg["config"] == {"url": url, "content_type": "json"}
    assert cfg["active"] is True


def test_webhook_config_includes_secret_when_given():
    secret = fake.sha256()
    cfg = build_webhook_config("https://x/webhook/pr-review", secret=secret)
    assert cfg["config"]["secret"] == secret


def test_find_existing_hook_id_matches_by_url():
    url = "https://x/webhook/pr-review"
    hooks = [
        {"id": 1, "config": {"url": "https://other/webhook/pr-review"}},
        {"id": 42, "config": {"url": url}},
    ]
    assert find_existing_hook_id(hooks, url) == 42
    assert find_existing_hook_id(hooks, "https://none") is None


# --- flawed_python_sample --------------------------------------------------


def test_flawed_sample_is_valid_python_with_findings():
    src = flawed_python_sample()
    compile(src, "<sample>", "exec")  # must parse
    assert "except:" in src  # at least one obvious smell present


# --- find_review_comment ---------------------------------------------------


def test_find_review_comment_returns_first_nonempty():
    comments = [{"body": "  "}, {"body": "Review: looks good"}]
    assert find_review_comment(comments)["body"] == "Review: looks good"


def test_find_review_comment_filters_by_author():
    comments = [
        {"body": "hi", "user": {"login": "someone-else"}},
        {"body": "review", "user": {"login": "me"}},
    ]
    assert find_review_comment(comments, author="me")["body"] == "review"


def test_find_review_comment_filters_by_since():
    comments = [
        {"body": "old", "created_at": "2020-01-01T00:00:00Z"},
        {"body": "new", "created_at": "2026-07-06T00:00:00Z"},
    ]
    got = find_review_comment(comments, since="2026-07-01T00:00:00Z")
    assert got["body"] == "new"


def test_find_review_comment_none_when_no_match():
    assert find_review_comment([], author="me") is None
    assert find_review_comment([{"body": ""}]) is None


# --- redact ----------------------------------------------------------------


def test_redact_hides_token_including_in_bearer():
    token = fake.sha256()
    cred = build_header_auth_credential(token)
    dumped = redact(json.dumps(cred), token)
    assert token not in dumped
    assert "***REDACTED***" in dumped


def test_redact_ignores_empty_secret():
    text = "nothing to hide"
    assert redact(text, "") == text
    assert redact(text) == text


def test_redact_handles_multiple_secrets():
    out = redact("a=1 b=2", "1", "2")
    assert "1" not in out and "2" not in out


# --- describe_live_plan ----------------------------------------------------


def test_live_plan_is_ordered_and_secret_free():
    token = fake.sha256()
    steps = describe_live_plan("o/r", "https://x/webhook/pr-review", "shiva-e2e-1")
    joined = "\n".join(steps)
    assert token not in joined
    assert steps[0].startswith("import")
    assert any("webhook" in s for s in steps)
    assert steps[-1] == "close the PR and delete the branch"


def test_live_plan_keep_changes_last_step():
    steps = describe_live_plan("o/r", "https://x", "b", keep=True)
    assert "--keep" in steps[-1]
