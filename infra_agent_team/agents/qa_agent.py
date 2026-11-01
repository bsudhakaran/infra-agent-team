"""QA agent: test design, test cases, execution and reporting.

The same data-driven test cases run twice:
  * phase "unit"  - against the generated code in the local emulator, BEFORE deploy
                    (nothing untested ever reaches the cloud), and
  * phase "e2e"   - against the real deployed URL, AFTER deploy.

Cases come from two sources:
  * baseline cases derived deterministically from the spec (smoke, contract, negative,
    security headers, integration with the report, and every worked example), and
  * LLM-designed cases (boundary values, odd inputs, per-operation edge cases).
"""

from __future__ import annotations

import html
import json
import py_compile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from .. import emulator
from ..llm import LLM
from ..log import say

AGENT = "qa-agent"

DESIGN_SYSTEM = """You are a senior QA engineer. Design tests for a deployed web app.
API contract:
  GET  /                -> HTML UI (200)
  GET  /health          -> {"status":"ok"}
  GET  /api/operations  -> {"operations":[{"name","label","description"}]}
  POST /api/submit      body {"operation": str, "input": str} -> 200 {"operation","result"};
                        400 {"error"} for unknown operation, invalid JSON, non-string input, or
                        input the operation rejects; 413 if input > 5000 chars
  GET  /api/report      -> {"total","errors","by_operation","recent","window"}
Return ONLY JSON: {"design_markdown": "<test design: scope, risks, approach, coverage>",
 "cases": [{"id": "TC-AI-001", "title": str, "category": "functional|boundary|negative",
            "method": "GET|POST", "path": "/api/submit", "body": {...} or null,
            "expect": {"status": int, "json_path_equals": {"result": <exact value>}}}]}
Write 6-12 cases focused on boundaries and edge cases per operation. Expected values must be
exactly what a correct implementation returns; only assert values you are certain of."""


# ----------------------------------------------------------------- execution
def _get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def _equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and not isinstance(actual, bool):
        return abs(actual - expected) < 1e-6
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(_equal(a, e) for a, e in zip(actual, expected))
    return actual == expected


def _send(base_url: str, case: dict[str, Any]) -> requests.Response:
    url = urljoin(base_url if base_url.endswith("/") else base_url + "/", case["path"].lstrip("/"))
    kwargs: dict[str, Any] = {"timeout": 15}
    if "body_raw" in case:
        kwargs["data"] = case["body_raw"]
        kwargs["headers"] = {"Content-Type": "application/json"}
    elif case.get("body") is not None:
        kwargs["json"] = case["body"]
    return requests.request(case["method"], url, **kwargs)


def run_case(base_url: str, case: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    result = {"id": case["id"], "title": case["title"], "category": case.get("category", ""), "status": "passed", "detail": ""}
    try:
        if case.get("kind") == "report_increments":
            before = _send(base_url, {"method": "GET", "path": "/api/report"}).json()["total"]
            _send(base_url, case)
            after = _send(base_url, {"method": "GET", "path": "/api/report"}).json()["total"]
            if before < case.get("window", 200) and after != before + 1:
                raise AssertionError(f"report total {before} -> {after}, expected +1")
        else:
            resp = _send(base_url, case)
            exp = case.get("expect", {})
            problems = []
            if "status" in exp and resp.status_code != exp["status"]:
                problems.append(f"status {resp.status_code} != {exp['status']} (body: {resp.text[:160]})")
            for text in exp.get("body_contains", []):
                if text not in resp.text:
                    problems.append(f"body missing {text!r}")
            for h, v in (exp.get("headers_include") or {}).items():
                if resp.headers.get(h, "").lower() != v.lower():
                    problems.append(f"header {h}={resp.headers.get(h)!r} != {v!r}")
            if exp.get("json_path_equals") or exp.get("operations_include"):
                data = resp.json()
                for p, v in (exp.get("json_path_equals") or {}).items():
                    try:
                        actual = _get_path(data, p)
                    except (KeyError, IndexError, ValueError, TypeError):
                        problems.append(f"json path '{p}' missing")
                        continue
                    if not _equal(actual, v):
                        problems.append(f"{p}={json.dumps(actual)} != {json.dumps(v)}")
                names = {o["name"] for o in data.get("operations", [])} if exp.get("operations_include") else set()
                missing = [n for n in exp.get("operations_include", []) if n not in names]
                if missing:
                    problems.append(f"operations missing {missing}")
            if problems:
                raise AssertionError("; ".join(problems))
    except AssertionError as e:
        result.update(status="failed", detail=str(e))
    except Exception as e:  # network errors, bad JSON, etc.
        result.update(status="error", detail=f"{type(e).__name__}: {e}")
    result["duration_ms"] = round((time.time() - started) * 1000, 1)
    return result


# ----------------------------------------------------------------- agent
class QAAgent:
    def __init__(self, llm: LLM):
        self.llm = llm

    def baseline_cases(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        names = [o["name"] for o in spec["operations"]]
        first = names[0]
        cases = [
            {"id": "TC-SMK-001", "title": "UI loads and shows the app title", "category": "smoke", "method": "GET", "path": "/",
             "expect": {"status": 200, "body_contains": [html.escape(spec["title"], quote=False)]}},
            {"id": "TC-SMK-002", "title": "Health endpoint is up", "category": "smoke", "method": "GET", "path": "/health",
             "expect": {"status": 200, "json_path_equals": {"status": "ok"}}},
            {"id": "TC-CON-001", "title": "All specified operations are exposed", "category": "contract", "method": "GET",
             "path": "/api/operations", "expect": {"status": 200, "operations_include": names}},
            {"id": "TC-CON-002", "title": "Report has the expected shape", "category": "contract", "method": "GET",
             "path": "/api/report", "expect": {"status": 200, "json_path_equals": {"window": 200}}},
            {"id": "TC-NEG-001", "title": "Unknown operation is rejected", "category": "negative", "method": "POST",
             "path": "/api/submit", "body": {"operation": "no_such_op", "input": "x"}, "expect": {"status": 400}},
            {"id": "TC-NEG-002", "title": "Malformed JSON body is rejected", "category": "negative", "method": "POST",
             "path": "/api/submit", "body_raw": "{not json", "expect": {"status": 400}},
            {"id": "TC-NEG-003", "title": "Non-string input is rejected", "category": "negative", "method": "POST",
             "path": "/api/submit", "body": {"operation": first, "input": 123}, "expect": {"status": 400}},
            {"id": "TC-NEG-004", "title": "Oversized input (5001 chars) is rejected", "category": "negative", "method": "POST",
             "path": "/api/submit", "body": {"operation": first, "input": "a" * 5001}, "expect": {"status": 413}},
            {"id": "TC-NEG-005", "title": "Unknown route returns 404", "category": "negative", "method": "GET",
             "path": "/does-not-exist", "expect": {"status": 404}},
            {"id": "TC-SEC-001", "title": "nosniff header present", "category": "security", "method": "GET", "path": "/",
             "expect": {"status": 200, "headers_include": {"X-Content-Type-Options": "nosniff"}}},
            {"id": "TC-INT-001", "title": "A submission is reflected in the report", "category": "integration",
             "kind": "report_increments", "method": "POST", "path": "/api/submit",
             "body": {"operation": first, "input": "integration check 1 2 3"}},
        ]
        for i, ex in enumerate(spec.get("examples", []), 1):
            cases.append({"id": f"TC-EX-{i:03d}", "title": f"{ex['operation']}({ex['input'][:30]!r}) == {json.dumps(ex['expected'])[:30]}",
                          "category": "functional", "method": "POST", "path": "/api/submit",
                          "body": {"operation": ex["operation"], "input": ex["input"]},
                          "expect": {"status": 200, "json_path_equals": {"result": ex["expected"]}}})
        return cases

    def design(self, spec: dict[str, Any], qa_dir: Path, feedback: str = "") -> list[dict[str, Any]]:
        qa_dir.mkdir(parents=True, exist_ok=True)
        user = "App spec:\n" + json.dumps({k: spec[k] for k in ("title", "operations", "examples")}, indent=2)
        if feedback:
            user += f"\n\nPrevious run feedback (fix wrong expectations if any):\n{feedback}"
        design_md, ai_cases = "", []
        try:
            out = self.llm.complete_json("test_design", DESIGN_SYSTEM, user, context={"spec": spec})
            design_md = str(out.get("design_markdown", ""))
            ai_cases = [c for c in out.get("cases", []) if self._valid_case(c, spec)]
        except Exception as e:
            say(AGENT, f"LLM test design unavailable ({e}); using baseline cases only")
        for i, c in enumerate(ai_cases, 1):
            c["id"] = f"TC-AI-{i:03d}"
            c.setdefault("category", "functional")
        cases = self.baseline_cases(spec) + ai_cases
        (qa_dir / "test_cases.json").write_text(json.dumps(cases, indent=2))
        (qa_dir / "test_design.md").write_text(self._design_doc(spec, design_md, cases))
        (qa_dir / "test_generated.py").write_text(_PYTEST_TEMPLATE)
        say(AGENT, f"Test design ready: {len(cases)} cases ({len(ai_cases)} LLM-designed)")
        return cases

    @staticmethod
    def _valid_case(c: Any, spec: dict[str, Any]) -> bool:
        if not isinstance(c, dict) or c.get("method") not in ("GET", "POST"):
            return False
        if not str(c.get("path", "")).startswith("/") or not isinstance(c.get("expect"), dict):
            return False
        if not isinstance(c["expect"].get("status"), int):
            return False
        body = c.get("body")
        return body is None or isinstance(body, dict)

    @staticmethod
    def _design_doc(spec, design_md, cases) -> str:
        rows = "\n".join(f"| {c['id']} | {c.get('category','')} | {c['title'].replace('|', '/')} | "
                         f"{c['method']} {c['path']} |" for c in cases)
        return (f"# Test design - {spec['title']}\n\n"
                f"Operations under test: {', '.join(o['name'] for o in spec['operations'])}\n\n"
                f"## Approach\n- Same cases run pre-deploy (local emulator) and post-deploy (live URL).\n"
                f"- Baseline cases are derived from the spec; LLM-designed cases add boundaries and edge cases.\n\n"
                f"{design_md}\n\n## Test cases\n\n| ID | Category | Title | Request |\n|---|---|---|---|\n{rows}\n")

    def static_checks(self, workspace: Path) -> list[dict[str, Any]]:
        results = []
        for f in sorted((workspace / "app").glob("*.py")):
            try:
                py_compile.compile(str(f), doraise=True, cfile=str(workspace / ".pyc_check"))
                results.append({"id": f"STATIC-{f.name}", "title": f"{f.name} compiles", "status": "passed", "detail": ""})
            except py_compile.PyCompileError as e:
                results.append({"id": f"STATIC-{f.name}", "title": f"{f.name} compiles", "status": "failed", "detail": str(e)})
        (workspace / ".pyc_check").unlink(missing_ok=True)
        for f in [workspace / "app" / "app_meta.json", workspace / "infra" / "terraform.tfvars.json"]:
            try:
                json.loads(f.read_text())
                results.append({"id": f"STATIC-{f.name}", "title": f"{f.name} is valid JSON", "status": "passed", "detail": ""})
            except Exception as e:
                results.append({"id": f"STATIC-{f.name}", "title": f"{f.name} is valid JSON", "status": "failed", "detail": str(e)})
        return results

    def execute(self, base_url: str, cases: list[dict[str, Any]], phase: str, qa_dir: Path,
                extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        results = list(extra or []) + [run_case(base_url, c) for c in cases]
        passed = sum(r["status"] == "passed" for r in results)
        summary = {"phase": phase, "target": base_url, "total": len(results), "passed": passed,
                   "failed": len(results) - passed, "ok": passed == len(results), "results": results}
        (qa_dir / f"results_{phase}.json").write_text(json.dumps(summary, indent=2))
        (qa_dir / f"report_{phase}.md").write_text(self._report_md(summary))
        say(AGENT, f"[{phase}] {passed}/{len(results)} passed against {base_url}")
        for r in results:
            if r["status"] != "passed":
                say(AGENT, f"  x {r['id']} {r['title']}: {r['detail']}")
        return summary

    def unit_phase(self, workspace: Path, cases: list[dict[str, Any]], qa_dir: Path) -> dict[str, Any]:
        static = self.static_checks(workspace)
        if any(r["status"] != "passed" for r in static):
            return self.execute("n/a", [], "unit", qa_dir, extra=static)
        emu = emulator.LocalEmulator(workspace / "app")
        url = emu.start()
        try:
            return self.execute(url, cases, "unit", qa_dir, extra=static)
        finally:
            emu.stop()

    def e2e_phase(self, base_url: str, cases: list[dict[str, Any]], qa_dir: Path, retries: int = 6) -> dict[str, Any]:
        # A brand-new API Gateway stage can take a few seconds to answer.
        base_url = base_url if base_url.endswith("/") else base_url + "/"
        for i in range(retries):
            try:
                if requests.get(urljoin(base_url, "health"), timeout=10).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(min(2 ** i, 15))
        return self.execute(base_url, cases, "e2e", qa_dir)

    @staticmethod
    def feedback(summary: dict[str, Any]) -> str:
        bad = [r for r in summary["results"] if r["status"] != "passed"]
        return "\n".join(f"- {r['id']} {r['title']}: {r['detail']}" for r in bad[:20])

    @staticmethod
    def _report_md(s: dict[str, Any]) -> str:
        rows = "\n".join(f"| {r['id']} | {'PASS' if r['status'] == 'passed' else r['status'].upper()} | "
                         f"{r['title'].replace('|', '/')} | {r.get('detail', '').replace('|', '/')[:120]} |" for r in s["results"])
        return (f"# QA report - {s['phase']}\n\nTarget: {s['target']}\n\n"
                f"**{s['passed']}/{s['total']} passed**\n\n| ID | Result | Title | Detail |\n|---|---|---|---|\n{rows}\n")


_PYTEST_TEMPLATE = '''"""Re-run the QA agent's cases yourself or in CI:
    BASE_URL=https://<api-id>.execute-api.<region>.amazonaws.com/ pytest test_generated.py
"""
import json
import os
from pathlib import Path

import pytest

from infra_agent_team.agents.qa_agent import run_case

CASES = json.loads((Path(__file__).parent / "test_cases.json").read_text())
BASE_URL = os.environ.get("BASE_URL")


@pytest.mark.skipif(not BASE_URL, reason="set BASE_URL to the deployed app URL")
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case):
    result = run_case(BASE_URL, case)
    assert result["status"] == "passed", result["detail"]
'''
