"""LangGraph orchestration of the agent team.

Deploy workflow
    credentials -> interpret -> generate -> unit_qa --fail--> generate (fix loop)
                                              |pass
                                             plan -> forecast -> approve (human gate)
                                                                    |yes
                                             e2e_qa <- apply <------+
                                     fail -> generate (fix + redeploy) | pass -> report
Destroy workflow
    cost_report -> approve_destroy (human gate) -> destroy -> orphan_sweep -> finalize
"""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import runstore
from .agents.coding_agent import CodingAgent, CodingError
from .agents.cost_agent import CostAgent
from .agents.credential_agent import CredentialAgent
from .agents.infra_agent import InfraAgent
from .agents.qa_agent import QAAgent
from .agents.router import RouterAgent
from .config import DEFAULT_BUDGET_USD, HARD_LIMIT_USD, MAX_ATTEMPTS
from .llm import LLM, get_llm
from .log import say
from .state import DeployState, DestroyState


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Team:
    """The five specialist agents (+ router) sharing one LLM and one credential vault."""

    def __init__(self, llm: LLM | None = None, credentials: CredentialAgent | None = None):
        self.llm = llm or get_llm()
        self.credentials = credentials or CredentialAgent()
        self.coding = CodingAgent(self.llm)
        self.infra = InfraAgent(self.credentials)
        self.qa = QAAgent(self.llm)
        self.cost = CostAgent()
        self.router = RouterAgent(self.llm)

    def aws_session(self, run: dict[str, Any]):
        return self.credentials.boto3_session(run["connection"]) if run.get("provider") == "aws" else None


def _node(name: str, fn: Callable[[dict], dict], persist: bool = True):
    """Run a node, persist non-secret state, and turn exceptions into a failed status."""
    def wrapped(state: dict) -> dict:
        try:
            update = fn(state) or {}
        except GraphBubbleUp:      # interrupts must propagate to the caller
            raise
        except Exception as e:
            say(name, f"ERROR: {e}")
            update = {"status": "failed", "error": f"{name}: {e}"}
        update.setdefault("events", [f"{_now()} {name}: {update.get('status', 'ok')}"])
        if persist and state.get("run_id"):
            runstore.save({**state, **update})
        return update
    return wrapped


def _approved(decision: Any) -> bool:
    return decision is True or str(decision).strip().lower() in ("y", "yes", "approve", "approved", "true")


# =================================================================== deploy
def build_deploy_graph(team: Team, checkpointer=None):
    def paths(state):
        rd = runstore.run_dir(state["run_id"])
        return rd / "workspace", rd / "qa"

    def credentials(state):
        ident = team.credentials.connect(state.get("connection"), state["run_id"])
        return {"identity": ident, "connection": ident["connection"], "provider": ident["provider"], "status": "connected"}

    def interpret(state):
        region = state["identity"].get("region") if state["provider"] != "local" else "us-east-1"
        return {"spec": team.coding.interpret(state["request"], state["provider"], region), "status": "spec_ready"}

    def generate(state):
        ws, _ = paths(state)
        attempt = state.get("attempts", 0) + 1
        say("coding-agent", f"Attempt {attempt}/{MAX_ATTEMPTS}")
        try:
            team.coding.generate(state["spec"], ws, state["run_id"], getpass.getuser(), state.get("feedback", ""))
            return {"attempts": attempt, "workspace": str(ws), "status": "generated"}
        except CodingError as e:
            say("coding-agent", f"Generated code rejected by policy: {e}")
            return {"attempts": attempt, "workspace": str(ws), "status": "codegen_rejected",
                    "feedback": f"- Code policy violation: {e}"}

    def unit_qa(state):
        ws, qa_dir = paths(state)
        cases = team.qa.design(state["spec"], qa_dir, state.get("feedback", ""))
        summary = team.qa.unit_phase(ws, cases, qa_dir)
        brief = {k: summary[k] for k in ("phase", "total", "passed", "failed", "ok")}
        if summary["ok"]:
            return {"unit_results": brief, "feedback": "", "status": "unit_passed"}
        return {"unit_results": brief, "feedback": team.qa.feedback(summary), "status": "unit_failed"}

    def plan(state):
        return {"plan": team.infra.plan(state["workspace"], state["connection"], state["provider"]), "status": "planned"}

    def forecast(state):
        return {"forecast": team.cost.forecast(state["plan"], state["spec"]), "status": "forecasted"}

    def approve(state):
        fc = state["forecast"]
        monthly, budget = fc["monthly_total"], state.get("budget", DEFAULT_BUDGET_USD)
        if monthly > HARD_LIMIT_USD:
            return {"status": "blocked", "error": f"Forecast ${monthly:.2f}/mo exceeds hard limit ${HARD_LIMIT_USD:.2f}/mo"}
        prior = state.get("approved_budget")
        if prior is not None and monthly <= prior + 1e-9:
            say("supervisor", "Redeploy within the previously approved forecast - continuing.")
            return {"status": "approved"}
        if state.get("auto_approve") and monthly <= budget and not fc["unpriced"]:
            say("supervisor", f"Auto-approved: ${monthly:.2f}/mo is within the ${budget:.2f}/mo budget.")
            return {"status": "approved", "approved_budget": monthly}
        decision = interrupt({
            "type": "approve_deploy", "run_id": state["run_id"], "forecast": fc,
            "plan_summary": state["plan"].get("summary"), "connection": state["connection"],
            "account": state["identity"].get("account_id"), "region": state["spec"]["region"],
            "over_budget": monthly > budget, "budget": budget,
        })
        if _approved(decision):
            return {"status": "approved", "approved_budget": monthly}
        return {"status": "rejected", "error": "Deployment rejected by user"}

    def apply(state):
        outputs = team.infra.apply(state["run_id"], state["workspace"], state["connection"], state["provider"])
        return {"outputs": outputs, "deployed_at": state.get("deployed_at") or _now(), "status": "applied"}

    def e2e_qa(state):
        _, qa_dir = paths(state)
        cases = json.loads((qa_dir / "test_cases.json").read_text())
        summary = team.qa.e2e_phase(state["outputs"]["app_url"], cases, qa_dir)
        brief = {k: summary[k] for k in ("phase", "total", "passed", "failed", "ok")}
        if summary["ok"]:
            return {"e2e_results": brief, "feedback": "", "status": "deployed"}
        return {"e2e_results": brief, "feedback": team.qa.feedback(summary), "status": "e2e_failed"}

    def report(state):
        rd = runstore.run_dir(state["run_id"])
        status = state.get("status")
        if status == "unit_failed" or status == "codegen_rejected":
            status = "qa_failed"
        lines = [f"# Run {state['run_id']}", "", f"- Request: {state.get('request')}", f"- Status: **{status}**",
                 f"- Connection: {state.get('connection')} ({state.get('provider')}) account {state.get('identity', {}).get('account_id')}"]
        if state.get("error"):
            lines.append(f"- Error: {state['error']}")
        if state.get("outputs"):
            lines.append(f"- App URL: {state['outputs'].get('app_url')}")
        for k in ("unit_results", "e2e_results"):
            r = state.get(k)
            if r:
                lines.append(f"- QA {r['phase']}: {r['passed']}/{r['total']} passed")
        fc = state.get("forecast")
        if fc:
            lines.append(f"- Forecast: ${fc['monthly_total']:.4f}/month, idle ${fc['idle_monthly']:.2f}/month, TTL {fc['ttl_hours']}h")
        lines += ["", "Next:", f"- Cost so far: `iat cost {state['run_id']}`", f"- Tear down: `iat destroy {state['run_id']}`"]
        (rd / "summary.md").write_text("\n".join(lines) + "\n")
        return {"status": status}

    # ---- routing
    def after_generate(state):
        return "unit_qa" if state["status"] == "generated" else ("generate" if state.get("attempts", 0) < MAX_ATTEMPTS else "report")

    def after_unit(state):
        if state["status"] == "unit_passed":
            return "plan"
        return "generate" if state["status"] == "unit_failed" and state.get("attempts", 0) < MAX_ATTEMPTS else "report"

    def ok_or_report(next_node):
        return lambda state: next_node if state.get("status") not in ("failed", "blocked", "rejected") else "report"

    def after_e2e(state):
        if state["status"] == "e2e_failed" and state.get("attempts", 0) < MAX_ATTEMPTS:
            return "generate"
        return "report"

    g = StateGraph(DeployState)
    for name, fn in [("credentials", credentials), ("interpret", interpret), ("generate", generate),
                     ("unit_qa", unit_qa), ("plan", plan), ("forecast", forecast), ("approve", approve),
                     ("apply", apply), ("e2e_qa", e2e_qa), ("report", report)]:
        g.add_node(name, _node(name, fn))
    g.add_edge(START, "credentials")
    g.add_conditional_edges("credentials", ok_or_report("interpret"), ["interpret", "report"])
    g.add_conditional_edges("interpret", ok_or_report("generate"), ["generate", "report"])
    g.add_conditional_edges("generate", after_generate, ["unit_qa", "generate", "report"])
    g.add_conditional_edges("unit_qa", after_unit, ["plan", "generate", "report"])
    g.add_conditional_edges("plan", ok_or_report("forecast"), ["forecast", "report"])
    g.add_conditional_edges("forecast", ok_or_report("approve"), ["approve", "report"])
    g.add_conditional_edges("approve", lambda s: "apply" if s["status"] == "approved" else "report", ["apply", "report"])
    g.add_conditional_edges("apply", ok_or_report("e2e_qa"), ["e2e_qa", "report"])
    g.add_conditional_edges("e2e_qa", after_e2e, ["generate", "report"])
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


# =================================================================== destroy
def build_destroy_graph(team: Team, checkpointer=None):
    def save(state, **kw):
        runstore.save({**state["run"], **kw})

    def cost_report(state):
        cost = team.cost.current(state["run"], team.aws_session(state["run"]))
        return {"cost": cost, "status": "costed"}

    def approve_destroy(state):
        if state.get("auto_approve"):
            return {"status": "approved"}
        run = state["run"]
        decision = interrupt({"type": "approve_destroy", "run_id": run["run_id"], "cost": state.get("cost"),
                              "connection": run.get("connection"), "app_url": run.get("outputs", {}).get("app_url")})
        return {"status": "approved"} if _approved(decision) else {"status": "kept", "error": "Destroy cancelled by user"}

    def destroy(state):
        remaining = team.infra.destroy(state["run"])
        return {"destroyed": not remaining, "status": "destroyed" if not remaining else "destroy_incomplete"}

    def orphan_sweep(state):
        return {"leftovers": team.cost.orphans(state["run"], team.aws_session(state["run"]))}

    def finalize(state):
        status = state.get("status")
        if status in ("destroyed", "destroy_incomplete"):
            run = {**state["run"], "status": status, "destroyed_at": _now() if state.get("destroyed") else None}
            final = team.cost.current(run, team.aws_session(run))
            save(state, status=status, destroyed_at=run["destroyed_at"], final_cost=final,
                 leftovers=state.get("leftovers", []))
            return {"cost": final}
        # cancelled or failed: leave the run's deployed status untouched, just record why
        save(state, last_destroy_error=state.get("error"))
        return {}

    def ok(next_node):
        return lambda s: next_node if s.get("status") not in ("failed", "kept") else "finalize"

    g = StateGraph(DestroyState)
    for name, fn in [("cost_report", cost_report), ("approve_destroy", approve_destroy), ("destroy", destroy),
                     ("orphan_sweep", orphan_sweep), ("finalize", finalize)]:
        g.add_node(name, _node(name, fn, persist=False))
    g.add_edge(START, "cost_report")
    g.add_conditional_edges("cost_report", ok("approve_destroy"), ["approve_destroy", "finalize"])
    g.add_conditional_edges("approve_destroy", ok("destroy"), ["destroy", "finalize"])
    g.add_edge("destroy", "orphan_sweep")
    g.add_edge("orphan_sweep", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


# =================================================================== runners
Approver = Callable[[dict[str, Any]], Any]


def _drive(graph, initial: dict[str, Any], thread_id: str, approver: Approver) -> dict[str, Any]:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    result = graph.invoke(initial, config)
    while result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        result = graph.invoke(Command(resume=approver(payload)), config)
    return result


def deploy(team: Team, request: str, connection: str | None, approver: Approver,
           auto_approve: bool = False, budget: float = DEFAULT_BUDGET_USD) -> dict[str, Any]:
    run_id = runstore.new_run_id()
    say("supervisor", f"Run {run_id}: {request}")
    initial: DeployState = {"run_id": run_id, "request": request, "connection": connection or team.credentials.default,
                            "attempts": 0, "auto_approve": auto_approve, "budget": budget, "status": "started",
                            "events": [f"{_now()} started"]}
    runstore.save(initial)
    return _drive(build_deploy_graph(team), initial, run_id, approver)


def destroy(team: Team, run_id: str, approver: Approver, auto_approve: bool = False) -> dict[str, Any]:
    run = runstore.load(run_id)
    if run.get("status") == "destroyed":
        say("supervisor", f"Run {run['run_id']} is already destroyed.")
        return {"run": run, "status": "destroyed", "cost": run.get("final_cost")}
    if not run.get("deployed_at"):
        say("supervisor", f"Run {run['run_id']} was never deployed; marking it closed.")
        runstore.save({**run, "status": "closed"})
        return {"run": run, "status": "closed"}
    say("supervisor", f"Destroying run {run['run_id']} ({run.get('connection')})")
    initial: DestroyState = {"run_id": run["run_id"], "run": run, "auto_approve": auto_approve, "status": "started", "events": []}
    return _drive(build_destroy_graph(team), initial, f"destroy-{run['run_id']}", approver)
