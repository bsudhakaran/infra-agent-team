"""Shared graph state.

Rule: nothing in this state is a secret. It is checkpointed by LangGraph and written to
run.json, so credentials stay inside the Credential agent and only the connection
*name* and the verified *identity* (account id / arn) travel through the graph.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class DeployState(TypedDict, total=False):
    run_id: str
    request: str
    connection: str
    provider: str                 # aws | gcp | azure | local
    identity: dict[str, Any]
    spec: dict[str, Any]
    workspace: str
    attempts: int
    feedback: str
    unit_results: dict[str, Any]
    plan: dict[str, Any]
    forecast: dict[str, Any]
    auto_approve: bool
    budget: float
    approved_budget: float
    outputs: dict[str, Any]
    e2e_results: dict[str, Any]
    deployed_at: str
    status: str
    error: str
    events: Annotated[list[str], operator.add]


class DestroyState(TypedDict, total=False):
    run_id: str
    run: dict[str, Any]
    cost: dict[str, Any]
    auto_approve: bool
    destroyed: bool
    leftovers: list[dict[str, Any]]
    status: str
    error: str
    events: Annotated[list[str], operator.add]
