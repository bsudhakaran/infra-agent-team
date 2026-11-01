"""Pluggable LLM layer.

Agents never import a vendor SDK. They call `llm.complete(task, system, user, context)`
or `llm.complete_json(...)`, where `task` names the job (intent, spec, operations_code,
test_design). Swap providers with env vars:

    LLM_PROVIDER = anthropic | openai | ollama | mock      (auto-detected if unset)
    LLM_MODEL    = model name for that provider
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .log import say


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an LLM response (handles ``` fences and chatter)."""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    decoder = json.JSONDecoder()
    for cand in candidates:
        for i, ch in enumerate(cand):
            if ch in "{[":
                try:
                    obj, _ = decoder.raw_decode(cand[i:])
                    return obj
                except json.JSONDecodeError:
                    continue
    raise LLMError("LLM response did not contain valid JSON")


def extract_code(text: str, lang: str = "python") -> str:
    m = re.search(rf"```(?:{lang})?\s*\n([\s\S]*?)```", text)
    return (m.group(1) if m else text).strip() + "\n"


class LLM:
    name = "base"

    def complete(self, task: str, system: str, user: str, context: dict | None = None) -> str:
        raise NotImplementedError

    def complete_json(self, task: str, system: str, user: str, context: dict | None = None) -> Any:
        text = self.complete(task, system, user, context)
        try:
            return extract_json(text)
        except LLMError:
            text = self.complete(task, system, user + "\n\nReturn ONLY valid JSON. No prose.", context)
            return extract_json(text)


class LangChainLLM(LLM):
    """Wraps any LangChain chat model."""

    def __init__(self, chat_model, name: str):
        self.model = chat_model
        self.name = name

    def complete(self, task, system, user, context=None) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = self.model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        content = resp.content
        if isinstance(content, list):  # some providers return content blocks
            content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        return content


_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1",
}


_MOCK_NOTICE = False


def _detect_provider() -> str:
    if os.environ.get("LLM_PROVIDER"):
        return os.environ["LLM_PROVIDER"].lower()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "mock"


def get_llm(provider: str | None = None) -> LLM:
    provider = (provider or _detect_provider()).lower()
    model = os.environ.get("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
    try:
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return LangChainLLM(ChatAnthropic(model=model, temperature=temperature, max_tokens=8000), f"anthropic:{model}")
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            return LangChainLLM(ChatOpenAI(model=model, temperature=temperature), f"openai:{model}")
        if provider == "ollama":
            from langchain_ollama import ChatOllama

            base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            return LangChainLLM(ChatOllama(model=model, temperature=temperature, base_url=base), f"ollama:{model}")
    except ImportError as e:
        raise LLMError(f"Provider '{provider}' needs its package: pip install 'infra-agent-team[{provider}]' ({e})")
    if provider == "mock":
        from .mock_llm import MockLLM

        global _MOCK_NOTICE
        if not _MOCK_NOTICE:
            say("llm", "No LLM_PROVIDER / API key found - using the deterministic mock LLM (offline demo mode).")
            _MOCK_NOTICE = True
        return MockLLM()
    raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use anthropic | openai | ollama | mock")
