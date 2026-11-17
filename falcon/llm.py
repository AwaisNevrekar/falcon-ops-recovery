"""One OpenAI-compatible client for Groq, Gemini or Ollama — and a clean 'off' mode.

LLMs only PLAN and EXPLAIN here. Every call can fail (no key, offline, timeout, rate limit, bad JSON);
then `chat_json` returns None and the caller uses its rule-based fallback. The API key is read from
the environment / .env only and is never logged, printed or returned.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config as C

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]},
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
        self._paused_until = 0.0
        self._lock = threading.Lock()
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
        return self.client is not None and self.calls < C.LLM_MAX_CALLS and time.time() >= self._paused_until

    def describe(self) -> dict:
        return {"provider": self.provider, "model": self.model, "enabled": self.enabled, "calls": self.calls,
                "mode": "LLM plans and explains" if self.enabled else "rule-based fallbacks (no LLM)"}

    def chat_json(self, system: str, user: str, tag: str = "") -> dict | None:
        if not self.enabled:
            return None
        if self.secret_check and self.secret_check(system + user):
            self.log.append({"tag": tag, "ok": False, "error": "PRIVACY_BLOCK: prompt contained protected data"})
            return None
        with self._lock:                                 # one call at a time: gentle on provider rate limits
            self.calls += 1
            t0, out, errors = time.time(), None, []
            queue = [self.model] + [m for m in self.models if m != self.model]
            tried = 0
            while queue and tried < 4 and out is None:
                model = queue.pop(0)
                tried += 1
                try:
                    r = self.client.chat.completions.create(
                        model=model, temperature=0, response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
                    out = parse_json(r.choices[0].message.content or "")
                    if out is None:
                        errors.append(f"{model}: reply was not a JSON object")
                        break
                    self.model = model                   # keep using the model that works
                except Exception as e:                   # network, auth, rate limit, unknown model ...
                    errors.append(f"{model}: {type(e).__name__}")
                    if type(e).__name__ == "RateLimitError" and tried == 1:
                        time.sleep(2.5)
                        queue.insert(0, model)           # same model once more after a short pause
            if out is None:
                self.failures += 1
                if self.failures >= 3:                   # circuit breaker for the LLM: rules only for a minute
                    self._paused_until, self.failures = time.time() + 60, 0
            else:
                self.failures = 0
            self.log.append({"tag": tag, "model": self.model, "ok": out is not None,
                             "ms": int((time.time() - t0) * 1000), "error": "; ".join(errors)})
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
