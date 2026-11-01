import json

import pytest

from infra_agent_team.agents.coding_agent import CodingAgent, CodingError


def base_spec(**over):
    s = {"blueprint": "aws_small_ui", "cloud": "aws", "app_name": "my-app", "title": "T", "region": "us-east-1",
         "operations": [{"name": "word_count"}], "examples": [{"operation": "word_count", "input": "a b", "expected": 2}]}
    s.update(over)
    return s


def test_spec_sanitises_app_name(mock_llm):
    spec = CodingAgent(mock_llm).validate_spec(base_spec(app_name="My App!! With Spaces"), "aws", "us-east-1")
    assert spec["app_name"] == "my-app-with-spaces"


def test_spec_rejects_region_outside_allow_list(mock_llm):
    with pytest.raises(CodingError, match="Region"):
        CodingAgent(mock_llm).validate_spec(base_spec(region="mars-1"), "aws", "us-east-1")


def test_spec_rejects_cloud_without_blueprint(mock_llm):
    with pytest.raises(CodingError, match="No blueprint for cloud 'gcp'"):
        CodingAgent(mock_llm).validate_spec(base_spec(cloud="gcp"), "gcp", "us-central1")


def test_spec_rejects_blueprint_connection_mismatch(mock_llm):
    with pytest.raises(CodingError, match="connection is gcp"):
        CodingAgent(mock_llm).validate_spec(base_spec(), "gcp", "us-central1")


def test_spec_drops_examples_for_unknown_ops(mock_llm):
    spec = CodingAgent(mock_llm).validate_spec(
        base_spec(examples=[{"operation": "nope", "input": "x", "expected": 1}]), "aws", "us-east-1")
    assert spec["examples"] == []


@pytest.mark.parametrize("code,msg", [
    ("import os\nOPERATIONS={'word_count':1}", "non-allowed"),
    ("from subprocess import run\nOPERATIONS={'word_count':1}", "non-allowed"),
    ("def f(t):\n    return open(t).read()\nOPERATIONS={'word_count':1}", "forbidden builtin 'open'"),
    ("def f(t):\n    return ().__class__\nOPERATIONS={'word_count':1}", "dunder"),
    ("def f(t):\n    return eval(t)\nOPERATIONS={'word_count':1}", "forbidden builtin 'eval'"),
    ("x = 1", "must define OPERATIONS"),
    ("OPERATIONS = {'other': 1}", "missing"),
    ("def broken(:\n", "syntax error"),
])
def test_code_policy_rejects_unsafe_code(code, msg):
    with pytest.raises(CodingError, match=msg):
        CodingAgent.check_code(code, ["word_count"])


def test_generate_builds_workspace(mock_llm, spec, tmp_path):
    files = CodingAgent(mock_llm).generate(spec, tmp_path / "ws", "rabc123", "tester")
    assert "app/operations.py" in files and "infra/main.tf" in files
    assert "app/operations_default.py" not in files
    tfvars = json.loads((tmp_path / "ws/infra/terraform.tfvars.json").read_text())
    assert tfvars["run_id"] == "rabc123" and tfvars["app_name"] == spec["app_name"]
