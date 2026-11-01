"""Deterministic stand-in LLM so the whole team runs offline, in CI and in tests.

It answers each agent task with what a well-behaved model would return. Set
LLM_PROVIDER=anthropic|openai|ollama for real generation.
"""

from __future__ import annotations

import json
import re

from .config import BLUEPRINT_DIR
from .llm import LLM

_DEFAULT_OPS = [
    ("word_count", "Word count", "Counts the words in the text", "free text"),
    ("char_count", "Character count", "Counts characters including spaces", "free text"),
    ("uppercase", "Uppercase", "Converts the text to upper case", "free text"),
    ("reverse_text", "Reverse text", "Reverses the text", "free text"),
    ("sum_numbers", "Sum numbers", "Adds comma/space separated numbers", "e.g. 4, 8, 15"),
    ("average_numbers", "Average numbers", "Mean of the numbers", "e.g. 2 4 6"),
    ("sort_numbers", "Sort numbers", "Sorts the numbers ascending", "e.g. 9 1 5"),
]
_EXAMPLES = [
    {"operation": "word_count", "input": "deploy test and destroy", "expected": 4},
    {"operation": "char_count", "input": "hello world", "expected": 11},
    {"operation": "uppercase", "input": "Agent Team", "expected": "AGENT TEAM"},
    {"operation": "reverse_text", "input": "abc", "expected": "cba"},
    {"operation": "sum_numbers", "input": "4, 8, 15, 16, 23, 42", "expected": 108},
    {"operation": "average_numbers", "input": "2 4 6", "expected": 4},
    {"operation": "sort_numbers", "input": "9 1 5", "expected": [1, 5, 9]},
]


class MockLLM(LLM):
    name = "mock"

    def complete(self, task, system, user, context=None) -> str:
        context = context or {}
        return getattr(self, f"_{task}")(context)

    def _intent(self, ctx) -> str:
        m = ctx.get("message", "").lower()
        run = re.search(r"\br[0-9a-f]{6}\b", m)
        rules = [("sweep", r"\b(sweep|expired|ttl)\b"),
                 ("destroy", r"\b(destroy|tear ?down|delete|clean ?up|remove)\b"),
                 ("cost", r"\b(cost|spend|bill|price|forecast|how much)\b"),
                 ("deploy", r"\b(deploy|create|build|launch|spin up|provision)\b"),
                 ("status", r"\b(status|list|show|running)\b")]
        intent = next((name for name, rx in rules if re.search(rx, m)), "help")
        return json.dumps({"intent": intent, "run_id": run.group(0) if run else None})

    def _spec(self, ctx) -> str:
        req = ctx.get("request", "").lower()
        ttl = 4
        m = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hr|h)\b", req)
        if m:
            ttl = float(m.group(1))
        return json.dumps({
            "blueprint": "aws_small_ui",
            "cloud": "gcp" if "gcp" in req or "google cloud" in req else "aws",
            "app_name": "input-ops",
            "title": "Input Operations",
            "description": "Enter some text or numbers, pick an operation, and see the result and a running report.",
            "region": ctx.get("region", "us-east-1"),
            "operations": [{"name": n, "label": l, "description": d, "input_hint": h} for n, l, d, h in _DEFAULT_OPS],
            "examples": _EXAMPLES,
            "usage_profile": {"requests_per_day": 1000, "avg_duration_ms": 120},
            "ttl_hours": ttl,
        })

    def _operations_code(self, ctx) -> str:
        code = (BLUEPRINT_DIR / "aws_small_ui" / "app" / "operations_default.py").read_text()
        return f"```python\n{code}```"

    def _test_design(self, ctx) -> str:
        design = (
            "### Scope\nFunctional behaviour of every operation, input validation, report integrity.\n\n"
            "### Risks\n- Numeric parsing (separators, negatives, decimals)\n- Empty and whitespace-only input\n"
            "- Unicode handling in text operations\n\n### Approach\nBoundary-value and equivalence partitioning per operation."
        )
        cases = [
            ("Word count of whitespace-only input is 0", "word_count", "   ", 200, 0),
            ("Word count collapses repeated spaces", "word_count", "a  b   c", 200, 3),
            ("Char count of empty string is 0", "char_count", "", 200, 0),
            ("Uppercase keeps digits and symbols", "uppercase", "abc-123!", 200, "ABC-123!"),
            ("Reverse handles unicode", "reverse_text", "naïve", 200, "evïan"),
            ("Sum handles negatives and decimals", "sum_numbers", "-1.5, 2.5, 10", 200, 11),
            ("Sum rejects non-numeric input", "sum_numbers", "1, two, 3", 400, None),
            ("Average of single number", "average_numbers", "7", 200, 7),
            ("Average rejects empty input", "average_numbers", "  ", 400, None),
            ("Sort keeps duplicates", "sort_numbers", "3 1 3 2", 200, [1, 2, 3, 3]),
        ]
        out = []
        for i, (title, op, inp, status, expected) in enumerate(cases, 1):
            exp = {"status": status}
            if expected is not None:
                exp["json_path_equals"] = {"result": expected}
            out.append({"id": f"TC-AI-{i:03d}", "title": title, "category": "boundary" if status == 200 else "negative",
                        "method": "POST", "path": "/api/submit", "body": {"operation": op, "input": inp}, "expect": exp})
        return json.dumps({"design_markdown": design, "cases": out})
