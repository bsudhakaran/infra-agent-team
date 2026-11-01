import pytest
from moto import mock_aws

from infra_agent_team.agents.credential_agent import CredentialAgent, CredentialError
from infra_agent_team.log import redact

MOTO_ACCOUNT = "123456789012"


def write_cfg(tmp_path, body):
    p = tmp_path / "clouds.yaml"
    p.write_text(body)
    return p


def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAMOCKMOCKMOCKMOCK")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "moto-secret-value-123456")
    monkeypatch.setenv("AWS_PROFILE_UNRELATED", "x")


def test_default_is_local_without_config(tmp_path):
    agent = CredentialAgent(tmp_path / "missing.yaml")
    assert agent.default == "local"
    assert agent.connect()["provider"] == "local"


def test_literal_secret_in_config_is_rejected(tmp_path):
    cfg = write_cfg(tmp_path, "connections:\n  bad:\n    provider: aws\n    auth:\n      aws_secret_access_key: abc\n")
    with pytest.raises(CredentialError, match="literal secret"):
        CredentialAgent(cfg)


@mock_aws
def test_aws_connect_and_scoped_env(tmp_path, monkeypatch):
    aws_env(monkeypatch)
    cfg = write_cfg(tmp_path, f"default: aws-dev\nconnections:\n  aws-dev:\n    provider: aws\n    region: us-east-1\n"
                              f"    auth: {{type: env}}\n    allowed_account_ids: ['{MOTO_ACCOUNT}']\n")
    agent = CredentialAgent(cfg)
    ident = agent.connect("aws-dev")
    assert ident["account_id"] == MOTO_ACCOUNT and "secret" not in str(ident).lower()
    env = agent.subprocess_env("aws-dev")
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAMOCKMOCKMOCKMOCK"
    assert "AWS_PROFILE_UNRELATED" not in env          # other cloud env stripped
    assert env["TF_IN_AUTOMATION"] == "1"
    assert "moto-secret-value-123456" not in redact("leaked: moto-secret-value-123456")


@mock_aws
def test_account_allow_list_blocks_wrong_account(tmp_path, monkeypatch):
    aws_env(monkeypatch)
    cfg = write_cfg(tmp_path, "connections:\n  prod:\n    provider: aws\n    auth: {type: env}\n"
                              "    allowed_account_ids: ['999999999999']\n")
    with pytest.raises(CredentialError, match="not in allowed_account_ids"):
        CredentialAgent(cfg).connect("prod")


def test_gcp_requires_key_file(tmp_path):
    cfg = write_cfg(tmp_path, "connections:\n  g:\n    provider: gcp\n    project: p\n"
                              "    auth: {type: service_account_file, credentials_file: /nope.json}\n")
    with pytest.raises(CredentialError, match="key file not found"):
        CredentialAgent(cfg).connect("g")


def test_redaction_patterns():
    text = "key AKIAABCDEFGHIJKLMNOP token=supersecretvalue123 sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"
    out = redact(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in out and "supersecretvalue123" not in out and "sk-ant" not in out
