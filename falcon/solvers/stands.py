"""Stand / gate allocation solver (OR-Tools CP-SAT). No LLM. Deterministic.

Given the ground visits and the double allocations, move as few aircraft as possible, only to stands
that fit the aircraft type and are free for the whole visit, preferring contact stands and short walks.
Aircraft already parked on a stand are never moved (that would need a tow).
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from .. import config as C


def _clash(v: dict, w: dict) -> bool:
    return v["start"] < w["end"] and w["start"] < v["end"]


def solve(visits: list[dict], stands: dict, conflicts: list[dict]) -> dict:
    by_id = {v["id"]: v for v in visits}
    movable = []
    for c in conflicts:
        for vid in (c["a"], c["b"]):
            v = by_id.get(vid)
            if v and not v["on_stand"] and vid not in movable:
                movable.append(vid)
    if not movable:
        return {"moves": [], "unresolved": [f"{c['a']}/{c['b']} on {c['stand']}" for c in conflicts]}
    fixed = [v for v in visits if v["id"] not in movable]
    order = sorted(stands)
    m = cp_model.CpModel()
    x, cost, unresolved = {}, [], []
    for vid in movable:
        v = by_id[vid]
        cur_walk = stands[v["stand"]]["walk"] if v["stand"] in stands else 0
        opts = []
        for si, sid in enumerate(order):
            s = stands[sid]
            if v["type"] not in s["types"] or any(w["stand"] == sid and _clash(v, w) for w in fixed):
                continue
            x[vid, sid] = m.new_bool_var(f"{vid}@{sid}")
            opts.append(x[vid, sid])
            if sid != v["stand"]:
                change = (C.COST["stand_change"] + max(0, s["walk"] - cur_walk) * v["pax"] // 5
                          + (0 if s["contact"] else 4 * v["pax"]) + (5000 if v["boarding"] else 0))
                cost.append(x[vid, sid] * (change * 100 + si))
        if opts:
            m.add_exactly_one(opts)
        else:
            unresolved.append(vid)
    for i, a in enumerate(movable):
        for b in movable[i + 1:]:
            if _clash(by_id[a], by_id[b]):
                for sid in order:
                    if (a, sid) in x and (b, sid) in x:
                        m.add(x[a, sid] + x[b, sid] <= 1)            # two visits never share a stand in time
    m.minimize(sum(cost))
    s = cp_model.CpSolver()
    s.parameters.num_workers = 1
    s.parameters.random_seed = 1
    s.parameters.max_time_in_seconds = 10
    if s.solve(m) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"moves": [], "unresolved": movable}
    moves = []
    for (vid, sid), var in x.items():
        v = by_id[vid]
        if s.value(var) and sid != v["stand"]:
            moves.append({"visit": vid, "arr": v["arr"], "dep": v["dep"], "ac": v["ac"], "from": v["stand"], "to": sid,
                          "pax": v["pax"], "boarding": v["boarding"], "contact": stands[sid]["contact"],
                          "why": f"{v['stand']} is double-allocated; {sid} fits a {v['type']} and is free "
                                 f"{C.hm(v['start'])}-{C.hm(v['end'])}"})
    return {"moves": sorted(moves, key=lambda mv: mv["visit"]), "unresolved": unresolved}
