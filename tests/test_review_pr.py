"""Tests for the Action CLI shell: config resolution, secrets, exit codes."""

import json

import pytest
import yaml

import review_pr
from shiva_agent import review
from shiva_agent.action import ActionError


@pytest.fixture
def config_file(tmp_path):
    config = {
        "categories": [
            {"id": "logical", "name": "Logical Review", "prompt": "Check the logic.", "enabled": True},
            {"id": "style", "name": "Code Style", "prompt": "Check the style.", "enabled": False},
        ],
        "exclude": ["*.lock"],
        "llm": {"provider": "openai"},
    }
    path = tmp_path / "shiva.config.yml"
    path.write_text(yaml.safe_dump(config))
    return path


# --- secrets ----------------------------------------------------------------


def test_credentials_are_redacted_from_logged_headers():
    headers = {"Authorization": "Bearer ghp_real", "x-api-key": "sk-real", "Accept": "json"}
    assert review_pr.redact(headers) == {
        "Authorization": "<redacted>",
        "x-api-key": "<redacted>",
        "Accept": "json",
    }


def test_a_dry_run_never_prints_a_secret(capsys, config_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("SHIVA_LLM_API_KEY", "sk-supersecret")
    monkeypatch.setattr(review_pr, "http_send", lambda spec: [])  # no PR files, no network
    code = review_pr.main(
        ["--repo", "ice1x/graphbook", "--pr", "42", "--dry-run",
         "--config", str(config_file), "--workspace", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "supersecret" not in out
    assert "<redacted>" in out


# --- config resolution ------------------------------------------------------


def test_only_enabled_categories_are_reviewed(config_file, tmp_path):
    settings = load(config_file, tmp_path)
    assert [c["name"] for c in settings["categories"]] == ["Logical Review"]


def load(config_file, workspace, repo="ice1x/graphbook"):
    return review_pr.load_settings(repo, config_file, workspace)


def test_the_target_repo_override_wins(config_file, tmp_path):
    (tmp_path / ".shiva.yml").write_text(
        yaml.safe_dump(
            {
                "categories": [{"id": "style", "enabled": True}],
                "conventions": "Never log secrets.",
                "llm": {"provider": "deepseek"},
            }
        )
    )
    settings = load(config_file, tmp_path)
    assert [c["name"] for c in settings["categories"]] == ["Logical Review", "Code Style"]
    assert settings["conventions"] == "Never log secrets."
    assert settings["llm"]["provider"] == "deepseek"


def test_defaults_apply_when_the_repo_ships_no_override(config_file, tmp_path):
    settings = load(config_file, tmp_path)
    assert settings["llm"]["provider"] == "openai"
    assert settings["exclude_globs"] == ["*.lock"]
    assert settings["conventions"] == ""


def test_a_malformed_override_is_rejected_with_a_clear_message(config_file, tmp_path):
    (tmp_path / ".shiva.yml").write_text(yaml.safe_dump({"categories": [{"id": "new"}]}))
    with pytest.raises(review.ConfigError) as exc:
        load(config_file, tmp_path)
    assert "new" in str(exc.value)


# --- event loading ----------------------------------------------------------


def test_the_event_payload_is_read_from_disk(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": {"number": 7}}))
    assert review_pr.load_event(str(path))["pull_request"]["number"] == 7


@pytest.mark.parametrize("path", [None, "", "/nonexistent/event.json"])
def test_a_missing_event_payload_is_an_actionable_error(path):
    with pytest.raises(ActionError) as exc:
        review_pr.load_event(path)
    assert "GITHUB_EVENT_PATH" in str(exc.value)


# --- exit codes -------------------------------------------------------------


def test_a_missing_github_token_exits_nonzero(capsys, config_file, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = review_pr.main(
        ["--repo", "ice1x/graphbook", "--pr", "1", "--config", str(config_file),
         "--workspace", str(tmp_path)]
    )
    assert code == 1
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_a_missing_repo_exits_nonzero(capsys, config_file, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    code = review_pr.main(["--pr", "1", "--config", str(config_file), "--workspace", str(tmp_path)])
    assert code == 1
    assert "--repo" in capsys.readouterr().err


def test_a_local_only_provider_exits_nonzero_with_guidance(
    capsys, tmp_path, monkeypatch
):
    config = {
        "categories": [{"id": "logical", "name": "L", "prompt": "p", "enabled": True}],
        "llm": {"provider": "ollama"},
    }
    path = tmp_path / "shiva.config.yml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    code = review_pr.main(
        ["--repo", "ice1x/graphbook", "--pr", "1", "--config", str(path),
         "--workspace", str(tmp_path)]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "runner cannot reach" in err
    assert "SHIVA_LLM_API_KEY" in err


# --- what a dry run does and does not do ------------------------------------


def test_a_dry_run_withholds_only_the_comment(monkeypatch, capsys):
    sent = []

    def fake_http(spec):
        sent.append(spec["url"])
        return {"ok": True}

    monkeypatch.setattr(review_pr, "http_send", fake_http)
    get_spec = {"method": "GET", "url": "https://api.github.com/repos/o/r/pulls/1/files", "headers": {}, "body": None}
    llm_spec = {"method": "POST", "url": "https://api.openai.com/v1/chat/completions", "headers": {}, "body": {"m": 1}}
    comment = {
        "method": "POST",
        "url": "https://api.github.com/repos/o/r/issues/1/comments",
        "headers": {"Authorization": "Bearer ghp_secret"},
        "body": {"body": "the findings"},
    }

    assert review_pr.dry_run_send(get_spec) == {"ok": True}
    assert review_pr.dry_run_send(llm_spec) == {"ok": True}
    assert review_pr.dry_run_send(comment) == {}

    assert sent == [get_spec["url"], llm_spec["url"]]  # the comment never went out
    out = capsys.readouterr().out
    assert "the findings" in out  # but it is shown for review
    assert "ghp_secret" not in out


@pytest.mark.parametrize(
    "spec,expected",
    [
        ({"method": "POST", "url": "https://api.github.com/repos/o/r/issues/1/comments"}, True),
        ({"method": "GET", "url": "https://api.github.com/repos/o/r/pulls/1/files"}, False),
        ({"method": "POST", "url": "https://api.openai.com/v1/chat/completions"}, False),
    ],
)
def test_only_the_comment_post_counts_as_a_mutation(spec, expected):
    assert review_pr.is_mutation(spec) is expected


def test_a_dry_run_reports_comments_as_withheld_not_posted():
    result = {"skipped": False, "reviewed_files": 3, "passes": 1, "comments_posted": 1}
    assert "withheld 1 comment" in review_pr.summarize(result, dry_run=True)
    assert "posted 1 comment" in review_pr.summarize(result, dry_run=False)


def test_a_skipped_run_says_so():
    assert "skipped" in review_pr.summarize({"skipped": True}, dry_run=False)
