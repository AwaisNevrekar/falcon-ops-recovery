"""Stage 2 — validate inputs: prompt-injection filter on free text and cross-feed consistency.

(Format and freshness checks live in the adapters.) The regex filter ALWAYS runs. A guard model
(llama-guard3 / Prompt Guard) is an optional second opinion: it can block more, never unblock.
"""
from __future__ import annotations

import re
from typing import Callable

from .. import config as C
from ..models import FeedResult

BLOCKED = "[BLOCKED: suspected prompt injection - text withheld from all agents]"
_PATTERNS = {
    "ignore-rules": r"(ignore|disregard|forget|bypass|override)\b.{0,40}\b(rule|instruction|guard|limit|polic|safety|check)",
    "release-aircraft": r"\brelease\b.{0,30}\baircraft\b|\baircraft\b.{0,30}\breleas",
    "skip-signoff": r"without\b.{0,30}\bsign.?off|\b(create|issue|forge|fake)\b.{0,20}\bsign.?off",
    "blanket-approval": r"\b(approve|authori[sz]e|accept)\b.{0,20}\b(all|every|any|automatically)\b",
    "role-play": r"\byou are now\b|\bact as\b|\bnew instructions?\b|\bsystem prompt\b|\bdeveloper mode\b",
    "markup": r"</?\s*(system|assistant|tool|instruction)s?\s*>|\[/?INST\]",
    "duty-limit": r"\b(exceed|extend|waive)\b.{0,30}\bduty\b",
    "arabic-ignore": r"تجاهل",
}
_COMPILED = {k: re.compile(v, re.IGNORECASE | re.DOTALL) for k, v in _PATTERNS.items()}


def scan_text(text: str) -> list[str]:
    """Names of the injection patterns found in a piece of free text (empty list = looks clean)."""
    text = re.sub(r"\s+", " ", text or "")
    return [name for name, rx in _COMPILED.items() if rx.search(text)]


def sanitize_notes(notes: list[str], guard: Callable[[str], bool] | None = None) -> tuple[list[str], list[dict]]:
    """Return (notes safe to show an agent, blocked findings). Blocked text is replaced, never forwarded."""
    safe, blocked = [], []
    for n in notes or []:
        hits = scan_text(n)
        if not hits and guard is not None:
            try:
                if guard(n):
                    hits = ["guard-model"]
            except Exception:           # a broken guard model must not open the gate; regex already passed
                pass
        if hits:
            blocked.append({"patterns": hits, "length": len(n)})
            safe.append(BLOCKED)
        else:
            safe.append(n[:300])
    return safe, blocked


def reconcile_eta(mov: FeedResult, atc: FeedResult) -> dict:
    """Cross-feed check of the airline ETA against the ATC ETA.

    Conflict (difference >= ETA_CONFLICT_MIN) or a stale airline record: use the LATER time for anything
    safety-relevant (crew legality, connections) and flag it, never silently pick one.
    """
    m = mov.value.get("eta") if isinstance(mov.value, dict) else None
    a = atc.value.get("eta") if isinstance(atc.value, dict) and atc.status == "OK" else None
    out = {"airline_eta": m, "atc_eta": a, "airline_status": mov.status, "atc_status": atc.status, "diff_min": None}
    if m is None and a is None:
        return {**out, "eta": None, "status": "MISSING", "basis": "no ETA from either feed"}
    if m is None:
        return {**out, "eta": a, "status": mov.status if mov.status != "OK" else "MISSING",
                "basis": "airline movement record unavailable - ATC ETA used"}
    if a is None:
        return {**out, "eta": m, "status": mov.status, "basis": "ATC ETA unavailable - airline ETA used"}
    diff = abs(m - a)
    out["diff_min"] = diff
    if mov.status == "STALE":
        return {**out, "eta": max(m, a), "status": "STALE",
                "basis": f"airline movement record is stale ({mov.age_s // 60} min old) - later ETA used"}
    if diff >= C.ETA_CONFLICT_MIN:
        return {**out, "eta": max(m, a), "status": "CONFLICT",
                "basis": f"airline and ATC ETAs disagree by {diff} min - later ETA used for safety decisions"}
    return {**out, "eta": max(m, a), "status": "OK", "basis": "airline and ATC ETAs agree"}
