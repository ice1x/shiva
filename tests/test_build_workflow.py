import json
from pathlib import Path

import pytest

from build_workflow import build_workflow

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "shiva.config.yml"


@pytest.fixture(scope="module")
def workflow():
    return build_workflow(CONFIG_PATH)


def node_by_type(workflow, node_type):
    return [n for n in workflow["nodes"] if n["type"] == node_type]


def test_workflow_is_json_serializable(workflow):
    json.dumps(workflow)


def test_has_expected_nodes(workflow):
    types = [n["type"] for n in workflow["nodes"]]
    assert "n8n-nodes-base.webhook" in types
    assert types.count("n8n-nodes-base.httpRequest") == 3  # fetch files, LLM, post comment
    assert "n8n-nodes-base.code" in types


def test_nodes_are_connected_in_a_chain(workflow):
    connections = workflow["connections"]
    names = {n["name"] for n in workflow["nodes"]}
    for source, targets in connections.items():
        assert source in names
        for branch in targets["main"]:
            for target in branch:
                assert target["node"] in names
    # every node except the last one has an outgoing connection
    assert len(connections) == len(workflow["nodes"]) - 1


def test_code_node_embeds_only_enabled_categories(workflow):
    code = node_by_type(workflow, "n8n-nodes-base.code")[0]
    script = code["parameters"]["pythonCode"]
    for name in [
        "Structural Review",
        "Logical Review",
        "Behavioral Review",
        "Security Review",
        "Performance Review",
    ]:
        assert name in script
    for name in [
        "Code Style Review",
        "Docstrings and Comments Review",
        "Messages Review",
        "Test Coverage Review",
    ]:
        assert name not in script


def test_llm_node_uses_current_anthropic_api(workflow):
    llm = [
        n
        for n in node_by_type(workflow, "n8n-nodes-base.httpRequest")
        if "anthropic" in n["parameters"]["url"]
    ][0]
    body = llm["parameters"]["jsonBody"]
    assert "claude-opus-4-8" in body
    assert '"adaptive"' in body  # adaptive thinking, no budget_tokens
    assert "budget_tokens" not in body
    headers = {
        h["name"]: h["value"]
        for h in llm["parameters"]["headerParameters"]["parameters"]
    }
    assert headers["anthropic-version"] == "2023-06-01"
