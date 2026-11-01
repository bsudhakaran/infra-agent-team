"""Supervisor / intent router: maps a free-text user message to the team workflow."""

from __future__ import annotations

from typing import Any

from ..llm import LLM

INTENTS = ("deploy", "cost", "destroy", "status", "sweep", "help")

SYSTEM = """You route requests for an infrastructure agent team. Classify the user's message.
Return ONLY JSON: {"intent": "deploy|cost|destroy|status|sweep|help", "run_id": "<id like r1a2b3c or null>"}
- deploy: build/create/deploy an app or infrastructure
- cost: check spend, bill, forecast or cost so far
- destroy: tear down, delete, clean up, destroy a deployment
- status: list or describe deployments
- sweep: clean up everything expired / past TTL
- help: anything else"""


class RouterAgent:
    def __init__(self, llm: LLM):
        self.llm = llm

    def route(self, message: str) -> dict[str, Any]:
        out = self.llm.complete_json("intent", SYSTEM, message, context={"message": message})
        intent = out.get("intent") if out.get("intent") in INTENTS else "help"
        run_id = out.get("run_id") if isinstance(out.get("run_id"), str) and out["run_id"].startswith("r") else None
        return {"intent": intent, "run_id": run_id}
