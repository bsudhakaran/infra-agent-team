"""Agent-prefixed console output with secret redaction.

Every line any agent prints goes through `redact`, so a credential that leaks into
Terraform output, an exception message or an LLM response never reaches the console
or the run files.
"""

from __future__ import annotations

import re

_registered: set[str] = set()

_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),                                    # AWS access key ids
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),                                      # OpenAI / Anthropic style keys
    re.compile(r"\bya29\.[A-Za-z0-9_\-\.]+"),                                       # GCP OAuth access tokens
]
_KV = re.compile(r"(?i)\b(secret|token|password|passwd|api[_-]?key|access[_-]?key)(\"?'?\s*[:=]\s*\"?'?)([^\s'\",}]{8,})")


def register_secret(value: str | None) -> None:
    """Tell the redactor about a concrete secret value so it is masked everywhere."""
    if value and len(value) >= 6:
        _registered.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    for s in sorted(_registered, key=len, reverse=True):
        text = text.replace(s, "***")
    for p in _PATTERNS:
        text = p.sub("***", text)
    return _KV.sub(lambda m: m.group(1) + m.group(2) + "***", text)


def say(agent: str, message: str) -> None:
    print(f"[{agent}] {redact(str(message))}", flush=True)
