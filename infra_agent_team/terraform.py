"""Thin, safe wrapper around the terraform (or OpenTofu) CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .log import redact, say

AGENT = "infra-agent"
_ATTRS_OF_INTEREST = ("instance_type", "instance_class", "memory_size", "billing_mode", "node_type", "allocated_storage")


class TerraformError(RuntimeError):
    pass


def find_binary() -> str | None:
    return shutil.which("terraform") or shutil.which("tofu")


class TerraformRunner:
    def __init__(self, workdir: Path, env: dict[str, str]):
        self.workdir = Path(workdir)
        self.env = env
        self.bin = find_binary()
        if not self.bin:
            raise TerraformError("terraform (or tofu) not found on PATH. Install it, or deploy with --connection local")

    def _run(self, *args: str, timeout: int = 1800, check: bool = True) -> str:
        cmd = [self.bin, *args]
        say(AGENT, "$ terraform " + " ".join(args))
        proc = subprocess.run(cmd, cwd=self.workdir, env=self.env, capture_output=True, text=True, timeout=timeout)
        out = redact(proc.stdout + proc.stderr)
        (self.workdir / "terraform.log").open("a").write(f"\n$ {' '.join(args)}\n{out}")
        if check and proc.returncode != 0:
            tail = "\n".join(out.strip().splitlines()[-25:])
            raise TerraformError(f"terraform {args[0]} failed (exit {proc.returncode}):\n{tail}")
        return proc.stdout

    def init(self) -> None:
        self._run("init", "-input=false", "-no-color")

    def validate(self) -> dict[str, Any]:
        out = self._run("validate", "-no-color", "-json", check=False)
        result = json.loads(out or "{}")
        if not result.get("valid", False):
            msgs = [d.get("summary", "") + ": " + d.get("detail", "") for d in result.get("diagnostics", [])]
            raise TerraformError("terraform validate failed:\n" + "\n".join(msgs))
        return result

    def plan(self) -> dict[str, Any]:
        self._run("plan", "-input=false", "-no-color", "-out=tfplan")
        shown = json.loads(self._run("show", "-json", "tfplan"))
        resources = []
        for rc in shown.get("resource_changes", []):
            if rc.get("mode") != "managed":
                continue
            after = (rc.get("change") or {}).get("after") or {}
            resources.append({
                "address": rc["address"], "type": rc["type"], "name": rc["name"],
                "actions": rc["change"]["actions"],
                "attributes": {k: after[k] for k in _ATTRS_OF_INTEREST if k in after},
            })
        return {"engine": "terraform", "resources": resources}

    def apply_plan(self) -> None:
        # Apply exactly the saved plan the human approved - not a fresh plan.
        self._run("apply", "-input=false", "-no-color", "-auto-approve", "tfplan")

    def outputs(self) -> dict[str, Any]:
        raw = json.loads(self._run("output", "-json") or "{}")
        return {k: v.get("value") for k, v in raw.items()}

    def destroy(self) -> None:
        self._run("destroy", "-input=false", "-no-color", "-auto-approve")

    def state_list(self) -> list[str]:
        out = self._run("state", "list", check=False)
        return [l for l in out.splitlines() if l.strip()]


_RESOURCE_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"', re.M)
_MEMORY_RE = re.compile(r'memory_size\s*=\s*(\d+)')


def static_plan(infra_dir: Path) -> dict[str, Any]:
    """Resource list parsed from .tf files - used by the local connection (no terraform needed)."""
    resources = []
    for tf in sorted(Path(infra_dir).glob("*.tf")):
        text = tf.read_text()
        for rtype, name in _RESOURCE_RE.findall(text):
            attrs = {}
            if rtype == "aws_lambda_function":
                m = _MEMORY_RE.search(text)
                attrs["memory_size"] = int(m.group(1)) if m else 128
            resources.append({"address": f"{rtype}.{name}", "type": rtype, "name": name,
                              "actions": ["create"], "attributes": attrs})
    return {"engine": "static", "resources": resources}
