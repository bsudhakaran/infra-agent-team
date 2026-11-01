"""End-to-end workflow tests: the whole team, local target and a simulated AWS target."""

import json
import os
import stat
import sys
import textwrap

import pytest
from moto import mock_aws

from infra_agent_team import emulator, graph, runstore
from infra_agent_team.agents.credential_agent import CredentialAgent
from infra_agent_team.mock_llm import MockLLM

REQUEST = "Deploy a small AWS UI that accepts user inputs, performs some operations and reports"
YES = lambda payload: True
NO = lambda payload: False


def team(llm=None, cfg=None):
    return graph.Team(llm=llm or MockLLM(), credentials=CredentialAgent(cfg) if cfg else None)


def test_local_deploy_cost_destroy():
    t = team()
    result = graph.deploy(t, REQUEST, "local", YES)
    assert result["status"] == "deployed", result.get("error")
    assert result["unit_results"]["ok"] and result["e2e_results"]["ok"]
    run = runstore.load(result["run_id"])
    assert run["deployed_at"] and "secret" not in json.dumps(run).lower()

    cost = t.cost.current(run)
    assert cost["actual_to_date"] == 0.0

    d = graph.destroy(t, result["run_id"], YES)
    assert d["status"] == "destroyed"
    assert runstore.load(result["run_id"])["status"] == "destroyed"


def test_rejected_approval_never_deploys():
    result = graph.deploy(team(), REQUEST, "local", NO)
    assert result["status"] == "rejected" and not result.get("deployed_at")


def test_cancelled_destroy_keeps_run_deployed():
    t = team()
    result = graph.deploy(t, REQUEST, "local", YES)
    graph.destroy(t, result["run_id"], NO)
    assert runstore.load(result["run_id"])["status"] == "deployed"
    emulator.stop(result["run_id"])


def test_auto_approve_respects_budget(monkeypatch):
    asked = []
    result = graph.deploy(team(), REQUEST, "local", lambda p: asked.append(p) or True, auto_approve=True, budget=0.0001)
    assert asked and asked[0]["over_budget"] and result["status"] == "deployed"
    emulator.stop(result["run_id"])


def test_hard_limit_blocks(monkeypatch):
    monkeypatch.setattr(graph, "HARD_LIMIT_USD", 0.0)
    result = graph.deploy(team(), REQUEST, "local", YES)
    assert result["status"] == "blocked" and not result.get("outputs")


class BuggyFirstLLM(MockLLM):
    """First code attempt has a wrong sum; the fix arrives once QA feedback is given."""

    def _operations_code(self, ctx):
        good = super()._operations_code(ctx)
        if not ctx.get("feedback"):
            return good.replace("return _clean(sum(_numbers(text)))", "return _clean(sum(_numbers(text)) + 1)")
        assert "sum_numbers" in ctx["feedback"]          # QA told the coder what broke
        return good


def test_qa_feedback_loop_fixes_code():
    result = graph.deploy(team(BuggyFirstLLM()), REQUEST, "local", YES)
    assert result["status"] == "deployed" and result["attempts"] == 2
    emulator.stop(result["run_id"])


class MaliciousLLM(MockLLM):
    def _operations_code(self, ctx):
        return "```python\nimport os\nOPERATIONS = {}\n```"


def test_unsafe_code_is_never_deployed():
    result = graph.deploy(team(MaliciousLLM()), REQUEST, "local", YES)
    assert result["status"] == "qa_failed" and result["attempts"] == 3 and not result.get("outputs")


def test_unsupported_cloud_fails_cleanly():
    result = graph.deploy(team(), "deploy a small ui on gcp", "local", YES)
    assert result["status"] == "failed" and "No blueprint for cloud 'gcp'" in result["error"]


# ------------------------------------------------------------------ simulated AWS
FAKE_TF = textwrap.dedent('''\
    #!{python}
    import json, os, sys, pathlib
    a = sys.argv[1:]
    state = pathlib.Path("fake.tfstate")
    pathlib.Path("env_seen.json").write_text(json.dumps({{"key": os.environ.get("AWS_ACCESS_KEY_ID")}}))
    cmd = a[0]
    if cmd == "validate": print(json.dumps({{"valid": True, "diagnostics": []}}))
    elif cmd == "plan": pathlib.Path("tfplan").write_text("plan")
    elif cmd == "show": print(json.dumps({{"resource_changes": [
        {{"address": "aws_lambda_function.app", "mode": "managed", "type": "aws_lambda_function", "name": "app",
          "change": {{"actions": ["create"], "after": {{"memory_size": 128}}}}}},
        {{"address": "aws_apigatewayv2_api.http", "mode": "managed", "type": "aws_apigatewayv2_api", "name": "http",
          "change": {{"actions": ["create"], "after": {{}}}}}},
        {{"address": "aws_dynamodb_table.submissions", "mode": "managed", "type": "aws_dynamodb_table", "name": "submissions",
          "change": {{"actions": ["create"], "after": {{"billing_mode": "PAY_PER_REQUEST"}}}}}}]}}))
    elif cmd == "apply": state.write_text("aws_lambda_function.app\\naws_dynamodb_table.submissions\\n")
    elif cmd == "output": print(json.dumps({{"app_url": {{"value": os.environ["FAKE_APP_URL"]}},
        "function_name": {{"value": "iat-fn"}}, "table_name": {{"value": "iat-tbl"}}, "api_id": {{"value": "abc123"}}}}))
    elif cmd == "destroy": state.unlink(missing_ok=True)
    elif cmd == "state": print(state.read_text() if state.exists() else "")
''')


@pytest.fixture
def fake_aws(tmp_path, monkeypatch, spec):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        # Windows ignores shebangs; shutil.which needs a PATHEXT extension, so wrap the script in a .cmd
        (bin_dir / "terraform.py").write_text(FAKE_TF.format(python=sys.executable))
        (bin_dir / "terraform.cmd").write_text(f'@"{sys.executable}" "%~dp0terraform.py" %*\n')
    else:
        tf = bin_dir / "terraform"
        tf.write_text(FAKE_TF.format(python=sys.executable))
        tf.chmod(tf.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAMOCKMOCKMOCKMOCK")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "moto-secret-value-123456")
    # Stand-in for the deployed API: the generated app running in the emulator.
    from infra_agent_team.agents.coding_agent import CodingAgent
    CodingAgent(MockLLM()).generate(spec, tmp_path / "stand_in", "rstand0", "t")
    emu = emulator.LocalEmulator(tmp_path / "stand_in/app")
    monkeypatch.setenv("FAKE_APP_URL", emu.start())
    cfg = tmp_path / "clouds.yaml"
    cfg.write_text("default: aws-test\nconnections:\n  aws-test:\n    provider: aws\n    region: us-east-1\n"
                   "    auth: {type: env}\n    allowed_account_ids: ['123456789012']\n")
    yield cfg
    emu.stop()


@mock_aws
def test_aws_path_with_fake_terraform(fake_aws):
    t = team(cfg=fake_aws)
    result = graph.deploy(t, REQUEST, None, YES)
    assert result["status"] == "deployed", result.get("error")
    assert result["identity"]["account_id"] == "123456789012"
    ws = runstore.run_dir(result["run_id"]) / "workspace" / "infra"
    resolved = t.credentials.boto3_session("aws-test").get_credentials().access_key
    assert json.loads((ws / "env_seen.json").read_text())["key"] == resolved   # creds reached terraform
    assert "moto-secret-value" not in (runstore.run_dir(result["run_id"]) / "run.json").read_text()

    d = graph.destroy(t, result["run_id"], YES)
    assert d["status"] == "destroyed" and d["leftovers"] == []
    assert not (ws / "fake.tfstate").exists()
