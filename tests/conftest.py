import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def iat_home(tmp_path, monkeypatch):
    monkeypatch.setenv("IAT_HOME", str(tmp_path / "iat"))
    monkeypatch.setenv("IAT_CLOUDS_CONFIG", str(tmp_path / "no-clouds.yaml"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for k in list(os.environ):
        if k.startswith("AWS_"):
            monkeypatch.delenv(k, raising=False)
    return tmp_path / "iat"


@pytest.fixture
def mock_llm():
    from infra_agent_team.mock_llm import MockLLM

    return MockLLM()


@pytest.fixture
def spec(mock_llm):
    from infra_agent_team.agents.coding_agent import CodingAgent

    return CodingAgent(mock_llm).interpret("deploy a small ui", "local", "us-east-1")
