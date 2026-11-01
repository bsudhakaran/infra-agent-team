"""Coding agent.

1. interpret(): turns the user's free-text request into a validated deployment *spec*
   (blueprint, app name, region, operations, worked examples, usage profile, TTL).
2. generate(): materialises a workspace - the vetted blueprint (handler, UI, Terraform)
   plus LLM-written business logic (operations.py) and generated tfvars/metadata.

Guardrail design: the LLM writes the parts where creativity is useful (business logic,
copy) while infrastructure comes from a reviewed Terraform blueprint that it can only
parameterise. Generated Python passes an AST policy check before anything runs.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import blueprints
from ..config import DEFAULT_TTL_HOURS
from ..llm import LLM, extract_code
from ..log import say

AGENT = "coding-agent"

ALLOWED_IMPORTS = {"math", "statistics", "re", "json", "string", "collections", "itertools",
                   "functools", "datetime", "decimal", "fractions", "textwrap", "unicodedata", "operator"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__", "input", "breakpoint",
                   "globals", "locals", "vars", "setattr", "delattr"}

SPEC_SYSTEM = """You are the Coding agent of an infrastructure team. Convert the user's deployment
request into a JSON spec. Pick the blueprint that fits from this catalog:
{catalog}

Return ONLY a JSON object with exactly these keys:
{{
  "blueprint": "<catalog key>",
  "cloud": "aws|gcp|azure",
  "app_name": "<2-20 chars, lowercase letters, digits, hyphens, starts with a letter>",
  "title": "<human title for the UI>",
  "description": "<one sentence shown under the title>",
  "region": "<cloud region; use {default_region} unless the user asked for another>",
  "operations": [{{"name": "<snake_case>", "label": "<UI label>", "description": "<what it does>", "input_hint": "<what to type>"}}],
  "examples": [{{"operation": "<name>", "input": "<string>", "expected": <exact JSON result>}}],
  "usage_profile": {{"requests_per_day": <int>, "avg_duration_ms": <int>}},
  "ttl_hours": <number, how long the environment should live; default {ttl}>
}}
Rules: 3-8 operations that each take ONE text input. Provide at least one example per operation
with an exactly computable expected result. If the user names no operations, choose sensible
text and number operations. If the request needs a cloud with no blueprint, still return the
closest blueprint but set "cloud" to what the user asked for."""

CODE_SYSTEM = """You are the Coding agent. Write the Python module `operations.py` for a Lambda app.
Contract:
- Each operation is a top-level function taking ONE str argument and returning a JSON-serializable value.
- Raise ValueError with a short user-facing message for invalid input.
- Define OPERATIONS = {{"<name>": {{"label": str, "description": str, "fn": <function>}}, ...}} with exactly
  these operation names: {names}
- Use only these standard-library modules: {allowed}. No I/O, no network, no eval/exec/open.
- Every example below must hold exactly: fn(input) == expected.
- Return integers for whole numbers (e.g. 6 not 6.0).
Return only the module in one ```python block."""

APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")


class CodingError(RuntimeError):
    pass


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:20].strip("-")
    if not s or not s[0].isalpha():
        s = ("app-" + s)[:20].strip("-")
    return s if len(s) >= 2 else "app"


class CodingAgent:
    def __init__(self, llm: LLM):
        self.llm = llm

    # ------------------------------------------------------------ interpret
    def interpret(self, request: str, provider: str, default_region: str) -> dict[str, Any]:
        system = SPEC_SYSTEM.format(catalog=blueprints.catalog_text(), default_region=default_region,
                                    ttl=DEFAULT_TTL_HOURS)
        raw = self.llm.complete_json("spec", system, f"User request:\n{request}",
                                     context={"request": request, "region": default_region})
        spec = self.validate_spec(raw, provider, default_region)
        say(AGENT, f"Spec: blueprint={spec['blueprint']} app={spec['app_name']} region={spec['region']} "
                   f"ops={[o['name'] for o in spec['operations']]}")
        return spec

    def validate_spec(self, raw: Any, provider: str, default_region: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise CodingError("Spec must be a JSON object")
        bp_name = raw.get("blueprint")
        if bp_name not in blueprints.BLUEPRINTS:
            raise CodingError(f"Unknown blueprint '{bp_name}'. Available: {', '.join(blueprints.BLUEPRINTS)}")
        bp = blueprints.BLUEPRINTS[bp_name]
        wanted_cloud = str(raw.get("cloud", bp["cloud"])).lower()
        if wanted_cloud != bp["cloud"]:
            raise CodingError(f"No blueprint for cloud '{wanted_cloud}' yet. Available: {blueprints.catalog_text()}")
        if provider not in ("local", bp["cloud"]):
            raise CodingError(f"Blueprint '{bp_name}' targets {bp['cloud']} but the connection is {provider}")

        region = raw.get("region") or default_region
        if provider == "local":
            region = region if region in bp["regions"] else bp["regions"][0]
        elif region not in bp["regions"]:
            raise CodingError(f"Region '{region}' not allowed for {bp_name}. Allowed: {bp['regions']}")

        ops = []
        seen = set()
        for o in raw.get("operations") or []:
            name = str(o.get("name", "")).strip()
            if not name.isidentifier() or name in seen:
                continue
            seen.add(name)
            ops.append({"name": name, "label": str(o.get("label") or name.replace("_", " ").title()),
                        "description": str(o.get("description", "")), "input_hint": str(o.get("input_hint", ""))})
        if not 1 <= len(ops) <= 12:
            raise CodingError("Spec needs 1-12 operations with valid snake_case names")
        examples = [e for e in raw.get("examples") or []
                    if isinstance(e, dict) and e.get("operation") in seen and isinstance(e.get("input"), str)
                    and "expected" in e]

        usage = raw.get("usage_profile") or {}
        app_name = raw.get("app_name") if APP_NAME_RE.match(str(raw.get("app_name", ""))) else _slug(str(raw.get("app_name") or raw.get("title") or "app"))
        try:
            ttl = float(raw.get("ttl_hours") or DEFAULT_TTL_HOURS)
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_HOURS
        return {
            "blueprint": bp_name,
            "cloud": bp["cloud"],
            "app_name": app_name,
            "title": str(raw.get("title") or app_name)[:80],
            "description": str(raw.get("description") or "")[:200],
            "region": region,
            "operations": ops,
            "examples": examples,
            "usage_profile": {
                "requests_per_day": max(0, int(usage.get("requests_per_day", 1000))),
                "avg_duration_ms": max(1, int(usage.get("avg_duration_ms", 120))),
            },
            "ttl_hours": min(max(ttl, 0.25), 24 * 30),
        }

    # ------------------------------------------------------------ generate
    def generate(self, spec: dict[str, Any], workspace: Path, run_id: str, owner: str,
                 feedback: str = "") -> list[str]:
        src = blueprints.path(spec["blueprint"])
        app_dir, infra_dir = workspace / "app", workspace / "infra"
        app_dir.mkdir(parents=True, exist_ok=True)
        infra_dir.mkdir(parents=True, exist_ok=True)

        # 1. vetted blueprint files (Terraform state files in infra/ are preserved across retries)
        for f in (src / "app").iterdir():
            if f.is_file() and f.name != "operations_default.py":
                shutil.copy2(f, app_dir / f.name)
        for f in (src / "infra").glob("*.tf"):
            shutil.copy2(f, infra_dir / f.name)

        # 2. LLM-generated business logic, policy-checked
        code = self._operations_code(spec, feedback)
        self.check_code(code, [o["name"] for o in spec["operations"]])
        (app_dir / "operations.py").write_text(code)

        # 3. generated metadata + Terraform variables
        (app_dir / "app_meta.json").write_text(json.dumps(
            {"app_name": spec["app_name"], "title": spec["title"], "description": spec["description"]}, indent=2))
        expires = datetime.now(timezone.utc) + timedelta(hours=spec["ttl_hours"])
        tfvars = {"region": spec["region"], "app_name": spec["app_name"], "run_id": run_id,
                  "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"), "owner": owner}
        (infra_dir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2))

        files = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*")
                       if p.is_file() and ".terraform" not in p.parts and "build" not in p.parts
                       and not p.name.startswith("terraform.tfstate") and p.name != "tfplan")
        say(AGENT, f"Generated workspace {workspace} ({len(files)} files)" + (" using QA feedback" if feedback else ""))
        return files

    def _operations_code(self, spec: dict[str, Any], feedback: str) -> str:
        names = [o["name"] for o in spec["operations"]]
        system = CODE_SYSTEM.format(names=", ".join(names), allowed=", ".join(sorted(ALLOWED_IMPORTS)))
        user = "Operations:\n" + json.dumps(spec["operations"], indent=2) + \
               "\n\nExamples (must pass):\n" + json.dumps(spec["examples"], indent=2)
        if feedback:
            user += f"\n\nThe previous version failed QA. Fix these problems:\n{feedback}"
        text = self.llm.complete("operations_code", system, user, context={"spec": spec, "feedback": feedback})
        return extract_code(text, "python")

    @staticmethod
    def check_code(code: str, required_ops: list[str]) -> None:
        """AST policy for LLM-written code: allow-listed imports, no dangerous builtins, no dunder access."""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            raise CodingError(f"operations.py has a syntax error: {e}") from e
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad = [a.name for a in node.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
            elif isinstance(node, ast.ImportFrom):
                bad = [node.module] if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS else []
            else:
                bad = []
            if bad:
                raise CodingError(f"operations.py imports non-allowed module(s): {bad}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                raise CodingError(f"operations.py calls forbidden builtin '{node.func.id}'")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise CodingError(f"operations.py accesses dunder attribute '{node.attr}'")
        has_ops = any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "OPERATIONS" for t in n.targets)
                      for n in tree.body)
        if not has_ops:
            raise CodingError("operations.py must define OPERATIONS")
        missing = [n for n in required_ops if f'"{n}"' not in code and f"'{n}'" not in code]
        if missing:
            raise CodingError(f"OPERATIONS is missing: {missing}")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
