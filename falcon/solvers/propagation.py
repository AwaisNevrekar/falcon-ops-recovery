"""Delay-propagation calculator (no LLM).

Follows aircraft rotations and crew pairings to work out the estimated off-block and arrival time of
every leg. The replay world uses the same function, so "what the calculator predicts" and "what the
simulated day does" can never drift apart. `ov` lets a caller ask what-if questions without changing
anything: {fid: {"hold": 12}} or {fid: {"turn_cut": 9}} or {fid: {"prev": None, "not_before": t}} (swap).
"""
from __future__ import annotations

from .. import config as C


def compute(legs: dict, order: list[str], ov: dict | None = None) -> dict[str, tuple[int, int]]:
    """Return {fid: (etd, eta)} for every non-cancelled leg, in schedule order."""
    ov = ov or {}
    out: dict[str, tuple[int, int]] = {}
    for f in order:
        leg = legs[f]
        o = ov.get(f, {})
        if leg["cancelled"] or o.get("cancelled"):
            continue
        if leg["atd"] is not None:
            etd = leg["atd"]
        else:
            etd = leg["std"] + leg["pdelay"] + o.get("pdelay", 0)
            p = o["prev"] if "prev" in o else leg["prev"]
            while p is not None and p not in out:          # skip cancelled legs of the rotation
                p = legs[p]["prev"]
            if p is not None:
                turn = leg["turn"]
                if leg["kind"] == "DEP":                   # hub turnaround can be compressed down to the floor
                    turn = max(leg["floor"], turn - leg["turn_cut"] - o.get("turn_cut", 0))
                etd = max(etd, out[p][1] + turn)
            for cp in leg["crew_prev"]:                    # crew arriving on another aircraft
                if cp in out:
                    etd = max(etd, out[cp][1] + C.CREW_CONNECT_MIN)
            etd += leg["hold"] + o.get("hold", 0)
            etd = max(etd, leg["not_before"], o.get("not_before", 0))
        eta = leg["ata"] if leg["ata"] is not None else etd + leg["block"] + leg["atfm"] + leg["stand_wait"]
        out[f] = (etd, eta)
    return out


def total_delay(legs: dict, times: dict[str, tuple[int, int]]) -> int:
    return sum(max(0, etd - legs[f]["std"]) for f, (etd, _) in times.items())


def chain(legs: dict, rot: dict, fid: str) -> list[str]:
    """`fid` and every later leg of the same aircraft."""
    r = rot.get(legs[fid]["ac"], [])
    return r[r.index(fid):] if fid in r else [fid]


def knock_on(legs: dict, order: list[str], ov: dict, base: dict | None = None) -> dict:
    """Effect of a what-if option on the whole network, compared with doing nothing."""
    base = base or compute(legs, order)
    new = compute(legs, order, ov)
    rows = []
    for f, (etd, eta) in new.items():
        if f in base and (etd != base[f][0] or eta != base[f][1]):
            rows.append({"fid": f, "delay_before": max(0, base[f][0] - legs[f]["std"]),
                         "delay_after": max(0, etd - legs[f]["std"]), "eta_before": base[f][1], "eta_after": eta})
    return {"delta_min": total_delay(legs, new) - total_delay(legs, base), "legs": rows, "times": new}


def reactionary(leg: dict) -> int:
    """Knock-on part of a leg's departure delay (total minus its own primary causes)."""
    own = leg["pdelay"] + leg["hold"] + leg["block_delay"]
    return max(0, leg["etd"] - leg["std"] - own)
