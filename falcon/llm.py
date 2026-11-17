"""One OpenAI-compatible client for Groq, Gemini or Ollama — and a clean 'off' mode.

LLMs only PLAN and EXPLAIN here. Every call can fail (no key, offline, timeout, rate limit, bad JSON);
then `chat_json` returns None and the caller uses its rule-based fallback. The API key is read from
the environment / .env only and is never logged, printed or returned.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config as C

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "models": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"]},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY",
               "models": ["gemini-2.5-flash", "gemini-2.0-flash"]},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "models": ["qwen3:8b", "qwen3:4b"]},
}


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", encoding="utf-8")
    except Exception:
        pass


def parse_json(text: str) -> dict | None:
    """Tolerant JSON-object parser (models sometimes wrap JSON in prose or code fences)."""
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        out = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None, client=None):
        _load_env()
        choice = (provider or os.environ.get("FALCON_LLM") or "").strip().lower()
        if not choice:
            choice = next((p for p in ("groq", "gemini") if os.environ.get(PROVIDERS[p]["key_env"])), "off")
        self.provider, self.client, self.models = choice, client, []
        self.calls = self.failures = 0
        self.log: list[dict] = []
        self.secret_check = None            # callable(text) -> True if the prompt would leak protected data
        if client is not None:
            self.models = [model or "test-model"]
        elif choice in PROVIDERS:
            cfg = PROVIDERS[choice]
            key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else "ollama"
            if key:
                try:
                    from openai import OpenAI
                    self.client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=C.LLM_TIMEOUT_S, max_retries=0)
                    override = model or os.environ.get("FALCON_LLM_MODEL")
                    self.models = ([override] if override else []) + cfg["models"]
                except Exception:
                    self.client = None
        self.model = self.models[0] if self.models else None

    @classmethod
    def off(cls) -> "LLM":
        return cls(provider="off")

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.calls < C.LLM_MAX_CALLS and self.failures < 3

    def describe(self) -> dict:
        return {"provider": self.provider, "model": self.model, "enabled": self.enabled, "calls": self.calls,
                "mode": "LLM plans and explains" if self.enabled else "rule-based fallbacks (no LLM)"}

    def chat_json(self, system: str, user: str, tag: str = "") -> dict | None:
        if not self.enabled:
            return None
        if self.secret_check and self.secret_check(system + user):
            self.log.append({"tag": tag, "ok": False, "error": "PRIVACY_BLOCK: prompt contained protected data"})
            return None
        self.calls += 1
        t0, out, err = time.time(), None, ""
        for attempt in range(2):
            try:
                r = self.client.chat.completions.create(
                    model=self.model, temperature=0, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
                out = parse_json(r.choices[0].message.content or "")
                err = "" if out is not None else "reply was not a JSON object"
                break
            except Exception as e:                       # network, auth, rate limit, unknown model ...
                err = type(e).__name__
                nxt = self.models.index(self.model) + 1 if self.model in self.models else len(self.models)
                if attempt == 0 and nxt < len(self.models):
                    self.model = self.models[nxt]        # try the next model once
                else:
                    break
        self.failures = 0 if out is not None else self.failures + 1
        self.log.append({"tag": tag, "model": self.model, "ok": out is not None, "ms": int((time.time() - t0) * 1000),
                         "error": err})
        return out


def guard_model():
    """Optional second opinion for the injection filter: llama-guard3 through Ollama (FALCON_GUARD=ollama)."""
    _load_env()
    if os.environ.get("FALCON_GUARD", "").lower() != "ollama":
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key="ollama", base_url=PROVIDERS["ollama"]["base_url"], timeout=5, max_retries=0)
    except Exception:
        return None

    def is_unsafe(text: str) -> bool:
        r = client.chat.completions.create(model="llama-guard3", temperature=0,
                                           messages=[{"role": "user", "content": text[:2000]}])
        return "unsafe" in (r.choices[0].message.content or "").lower()
    return is_unsafe
