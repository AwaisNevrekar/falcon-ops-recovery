"""Crew legality calculator (no LLM). Demo limits from config.py — not a legal statement.

It answers two questions and nothing else:
  check()          is this crew member legal for these legs at these (estimated) times?
  legal_options()  which standby crew could LEGALLY take over? Illegal people are never returned.
"""
from __future__ import annotations

from .. import config as C


def check(member: dict, leg_times: list[tuple[str, int, int]], now: int, duty_start: int | None = None) -> dict:
    """leg_times = [(fid, etd, eta)] for every leg the person still has to fly today."""
    start = duty_start if duty_start is not None else member["duty_start"]
    if start is None:
        start = now
    limit = start + C.MAX_DUTY_MIN
    end = max((eta for _, _, eta in leg_times), default=now) + C.POST_FLIGHT_MIN
    sectors = max(len(member["legs"]), len(leg_times))
    reasons = []
    if member["rest_before"] < C.MIN_REST_MIN:
        reasons.append(f"REST: {member['rest_before']} min rest before duty, minimum {C.MIN_REST_MIN}")
    if sectors > C.MAX_SECTORS:
        reasons.append(f"SECTORS: {sectors} sectors, maximum {C.MAX_SECTORS}")
    if end > limit:
        reasons.append(f"DUTY: duty would end {C.hm(end)}, limit {C.hm(limit)} ({end - limit} min over)")
    return {"crew": member["id"], "role": member["role"], "legal": not reasons, "reasons": reasons,
            "margin_min": limit - end, "duty_end": end, "limit": limit}


def legal_options(role: str, leg_times: list[tuple[str, int, int]], standby: list[dict], now: int) -> dict:
    """Standby crew of `role` who can legally fly `leg_times`. Returns legal options only (+ why others fail)."""
    options, rejected = [], []
    for m in sorted(standby, key=lambda m: m["id"]):
        if m["role"] != role or m["legs"] or not m["standby"]:
            continue
        ready = max(now, m["available_from"] or now) + C.STANDBY_CALLOUT_MIN
        first_etd = leg_times[0][1] if leg_times else now
        shift = max(0, ready - first_etd)                      # the flight waits for the standby if needed
        res = check(m, [(f, etd + shift, eta + shift) for f, etd, eta in leg_times], now, duty_start=now)
        if res["legal"]:
            options.append({"crew": m["id"], "role": role, "ready_at": ready, "extra_delay_min": shift,
                            "margin_min": res["margin_min"], "limit": res["limit"]})
        else:
            rejected.append({"crew": m["id"], "reasons": res["reasons"]})
    options.sort(key=lambda o: (o["extra_delay_min"], -o["margin_min"], o["crew"]))
    return {"options": options, "rejected": rejected}
