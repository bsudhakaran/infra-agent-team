"""Persists each run's non-secret state to .iat/runs/<run_id>/run.json.

The run file is what lets `iat cost`, `iat destroy` and `iat sweep` work days later in a
fresh process, and the Terraform state next to it is what makes destroy possible.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from .config import runs_dir

_SKIP_KEYS = {"events"}


def new_run_id() -> str:
    # short, lowercase, DNS/Lambda/DynamoDB-safe
    return "r" + secrets.token_hex(3)


def run_dir(run_id: str) -> Path:
    d = runs_dir() / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(state: dict[str, Any]) -> Path:
    run_id = state["run_id"]
    path = run_dir(run_id) / "run.json"
    existing = load(run_id) if path.exists() else {}
    data = {**existing, **{k: v for k, v in state.items() if k not in _SKIP_KEYS}}
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("created_at", data["updated_at"])
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def load(run_id: str) -> dict[str, Any]:
    if run_id == "latest":
        runs = list_runs()
        if not runs:
            raise FileNotFoundError("No runs yet. Start one with: iat deploy \"<request>\"")
        return runs[0]
    path = runs_dir() / run_id / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown run '{run_id}'. See: iat status")
    return json.loads(path.read_text())


def list_runs() -> list[dict[str, Any]]:
    runs = []
    for p in runs_dir().glob("*/run.json"):
        try:
            runs.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)
