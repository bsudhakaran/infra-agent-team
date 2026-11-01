"""Infra build agent: plan, apply and destroy the workspace's infrastructure.

Cloud connections go through Terraform (init -> validate -> plan -> [approval] -> apply
the saved plan). The `local` connection "deploys" to the in-process emulator so the
whole team can be exercised without a cloud account.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import emulator
from ..log import say
from ..terraform import TerraformRunner, static_plan
from .credential_agent import CredentialAgent

AGENT = "infra-agent"


class InfraAgent:
    def __init__(self, credentials: CredentialAgent):
        self.credentials = credentials

    def _runner(self, workspace: Path, connection: str) -> TerraformRunner:
        return TerraformRunner(Path(workspace) / "infra", self.credentials.subprocess_env(connection))

    def plan(self, workspace: str, connection: str, provider: str) -> dict[str, Any]:
        if provider == "local":
            plan = static_plan(Path(workspace) / "infra")
        else:
            tf = self._runner(Path(workspace), connection)
            tf.init()
            tf.validate()
            plan = tf.plan()
        counts: dict[str, int] = {}
        for r in plan["resources"]:
            key = "/".join(r["actions"])
            counts[key] = counts.get(key, 0) + 1
        plan["summary"] = counts
        say(AGENT, f"Plan ({plan['engine']}): {len(plan['resources'])} resources {counts}")
        return plan

    def apply(self, run_id: str, workspace: str, connection: str, provider: str) -> dict[str, Any]:
        if provider == "local":
            url = emulator.LocalEmulator(Path(workspace) / "app").start(key=run_id)
            outputs = {"app_url": url, "function_name": f"local-{run_id}", "table_name": "in-memory", "api_id": "local"}
        else:
            tf = self._runner(Path(workspace), connection)
            tf.apply_plan()
            outputs = tf.outputs()
        say(AGENT, f"Deployed. App URL: {outputs.get('app_url')}")
        return outputs

    def destroy(self, run: dict[str, Any]) -> list[str]:
        """Destroy and return whatever Terraform still tracks afterwards (should be empty)."""
        if run.get("provider") == "local":
            stopped = emulator.stop(run["run_id"])
            say(AGENT, "Local emulator " + ("stopped." if stopped else "was not running in this process; nothing to stop."))
            return []
        tf = self._runner(Path(run["workspace"]), run["connection"])
        tf.init()
        tf.destroy()
        remaining = tf.state_list()
        say(AGENT, "Destroy complete." if not remaining else f"Still in state after destroy: {remaining}")
        return remaining
