"""Guards on the shipped Action wiring — action.yml, the workflow, .shiva.yml.

These files are the difference between "works on my laptop with a tunnel" and
"runs on every PR", and nothing else fails when they drift, so they are checked
here: the action must invoke a script that exists, the workflow must trigger on
the events the agent considers reviewable, and this repo's own override must
resolve to a provider a runner can actually reach.
"""

from pathlib import Path

import yaml

from shiva_agent import review
from shiva_agent.action import require_runner_reachable

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION = yaml.safe_load((REPO_ROOT / "action.yml").read_text())
WORKFLOW = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "shiva-review.yml").read_text())
CONFIG = yaml.safe_load((REPO_ROOT / "shiva.config.yml").read_text())
OVERRIDE = yaml.safe_load((REPO_ROOT / ".shiva.yml").read_text())


# --- action.yml -------------------------------------------------------------


def test_the_action_runs_a_script_that_exists():
    steps = ACTION["runs"]["steps"]
    run = " ".join(step.get("run", "") for step in steps)
    assert "scripts/review_pr.py" in run
    assert (REPO_ROOT / "scripts" / "review_pr.py").exists()


def test_the_action_takes_a_key_and_a_token():
    assert set(ACTION["inputs"]) >= {"llm-api-key", "github-token"}


def test_the_key_reaches_the_script_under_the_env_var_the_code_reads():
    from shiva_agent.action import LLM_KEY_ENV

    env = [step.get("env", {}) for step in ACTION["runs"]["steps"]]
    assert any(LLM_KEY_ENV in e for e in env)


def test_the_action_is_composite_so_a_target_repo_can_use_it_directly():
    assert ACTION["runs"]["using"] == "composite"


# --- the workflow -----------------------------------------------------------


def test_the_workflow_triggers_on_every_reviewable_pull_request_event():
    # PyYAML reads the `on:` key as the boolean True.
    triggers = WORKFLOW[True]["pull_request"]["types"]
    assert set(triggers) == set(review.REVIEWABLE_ACTIONS)


def test_the_workflow_may_post_a_comment():
    assert WORKFLOW["permissions"]["pull-requests"] == "write"


def test_the_workflow_uses_the_action_from_this_checkout():
    steps = WORKFLOW["jobs"]["review"]["steps"]
    assert any(step.get("uses") == "./" for step in steps)


def test_the_review_is_skipped_rather_than_failing_when_no_key_is_configured():
    steps = WORKFLOW["jobs"]["review"]["steps"]
    guarded = [step for step in steps if step.get("uses") == "./"]
    assert all("LLM_KEY != ''" in step.get("if", "") for step in guarded)


# --- this repo's own override ----------------------------------------------


def test_the_shipped_override_is_a_valid_config():
    review.validate_config(review.merge_config(CONFIG, OVERRIDE), require_enabled=True)


def test_the_override_resolves_to_a_provider_a_runner_can_reach():
    assert require_runner_reachable(review.resolve_llm(CONFIG, OVERRIDE)) is None


def test_the_default_provider_stays_local_and_free():
    # The default must remain the keyless local one: a fresh clone costs nothing.
    llm = review.resolve_llm(CONFIG)
    assert llm["provider"] == review.DEFAULT_LLM_PROVIDER
    assert llm["auth"] == "none"
