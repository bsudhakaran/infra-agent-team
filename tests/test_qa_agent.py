from infra_agent_team import emulator
from infra_agent_team.agents.coding_agent import CodingAgent
from infra_agent_team.agents.qa_agent import QAAgent, _equal, run_case


def test_equal_tolerates_float_noise():
    assert _equal(0.1 + 0.2, 0.3) and _equal([1, 2.0], [1, 2])
    assert not _equal("1", 1) and not _equal([1, 2], [1, 2, 3])


def test_baseline_cases_pass_on_blueprint(mock_llm, spec, tmp_path):
    CodingAgent(mock_llm).generate(spec, tmp_path / "ws", "rtest01", "t")
    qa = QAAgent(mock_llm)
    cases = qa.design(spec, tmp_path / "qa")
    summary = qa.unit_phase(tmp_path / "ws", cases, tmp_path / "qa")
    assert summary["ok"], qa.feedback(summary)
    assert (tmp_path / "qa/test_design.md").exists() and (tmp_path / "qa/test_generated.py").exists()


def test_wrong_expectation_is_reported(mock_llm, spec, tmp_path):
    CodingAgent(mock_llm).generate(spec, tmp_path / "ws", "rtest02", "t")
    emu = emulator.LocalEmulator(tmp_path / "ws/app")
    url = emu.start()
    try:
        case = {"id": "X", "title": "x", "method": "POST", "path": "/api/submit",
                "body": {"operation": "word_count", "input": "a b"}, "expect": {"status": 200, "json_path_equals": {"result": 3}}}
        r = run_case(url, case)
        assert r["status"] == "failed" and "result=2 != 3" in r["detail"]
    finally:
        emu.stop()


def test_invalid_llm_cases_are_dropped(spec):
    assert not QAAgent._valid_case({"method": "DELETE", "path": "/", "expect": {"status": 200}}, spec)
    assert not QAAgent._valid_case({"method": "GET", "path": "nope", "expect": {"status": 200}}, spec)
    assert QAAgent._valid_case({"method": "GET", "path": "/health", "expect": {"status": 200}}, spec)
