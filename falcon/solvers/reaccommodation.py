"""Re-accommodation solver (OR-Tools CP-SAT, PuLP fallback). No LLM. Same inputs -> same result.

Implements the published priority table (ARCHITECTURE.md section 6):
  1. Hard constraints: seat capacity, connection/ready time, cabin rules, PNR group kept together,
     PRM only where assistance is available, unaccompanied minors only same-day with an escort.
  2. Priority order: (a) PRM and unaccompanied minors, (b) last onward connection of the day,
     (c) families with children, (d) tight onward connections, (e) everyone else by earliest original
     departure, then booking time. A lower class can never take a seat from a higher class, because
     the classes are solved in order on the remaining capacity.
  3. Cabin / loyalty only break ties after (a)-(e).
  4. Objective inside a class: minimise total arrival delay at destination (+ long-delay and downgrade penalties).
  5. Every assignment says why (rule ids + delay at destination).
"""
from __future__ import annotations

from collections import defaultdict

from .. import config as C

CLASSES = [("a", "reduced mobility / unaccompanied minor"), ("b", "last onward connection of the day"),
           ("c", "family with children"), ("d", "tight onward connection"),
           ("e", "earliest original departure, then booking time")]
_UNASSIGNED = 10 ** 9


def priority_class(g: dict) -> str:
    if "WCHR" in g["ssr"] or "UMNR" in g["ssr"]:
        return "a"
    if g.get("onward") == "last":
        return "b"
    if g.get("family"):
        return "c"
    if g.get("onward") == "tight":
        return "d"
    return "e"


def infeasible_reason(g: dict, f: dict) -> str:
    """'' if group g may be put on flight f (seats are checked by the solver)."""
    if f["etd"] < g["ready"]:
        return "READY_TIME"
    if "WCHR" in g["ssr"] and not f.get("prm_assist", False):
        return "NO_PRM_ASSISTANCE"
    if "UMNR" in g["ssr"] and (f.get("next_day") or not f.get("umnr_escort", False)):
        return "UMNR_NEEDS_SAME_DAY_ESCORT"
    return ""


def _solve_cpsat(cands, sizes, cap):
    from ortools.sat.python import cp_model
    m = cp_model.CpModel()
    x = [m.new_bool_var(f"x{i}") for i in range(len(cands))]
    by_g, by_cap = defaultdict(list), defaultdict(list)
    for i, (gi, fid, cab, _) in enumerate(cands):
        by_g[gi].append(i)
        by_cap[(fid, cab)].append(i)
    for idx in by_g.values():
        m.add(sum(x[i] for i in idx) <= 1)                               # a group travels together, on one flight
    for key, idx in by_cap.items():
        m.add(sum(x[i] * sizes[cands[i][0]] for i in idx) <= max(0, cap[key]))   # seats are never exceeded
    m.minimize(sum(x[i] * (c[3] - _UNASSIGNED * sizes[c[0]]) for i, c in enumerate(cands)))
    s = cp_model.CpSolver()
    s.parameters.num_workers = 1                                         # single thread + fixed seed = reproducible
    s.parameters.random_seed = 1
    s.parameters.max_time_in_seconds = 20
    status = s.solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return []
    return [i for i in range(len(cands)) if s.value(x[i])]


def _solve_pulp(cands, sizes, cap):
    import pulp
    m = pulp.LpProblem("reacc", pulp.LpMinimize)
    if hasattr(m, "add_variable"):                                       # PuLP 4.x
        x = [m.add_variable(f"x{i}", cat="Binary") for i in range(len(cands))]
    else:                                                                # PuLP 2.x / 3.x
        x = [pulp.LpVariable(f"x{i}", cat="Binary") for i in range(len(cands))]
    by_g, by_cap = defaultdict(list), defaultdict(list)
    for i, (gi, fid, cab, _) in enumerate(cands):
        by_g[gi].append(i)
        by_cap[(fid, cab)].append(i)
    m += pulp.lpSum(x[i] * (c[3] - _UNASSIGNED * sizes[c[0]]) for i, c in enumerate(cands))
    for idx in by_g.values():
        m += pulp.lpSum(x[i] for i in idx) <= 1
    for key, idx in by_cap.items():
        m += pulp.lpSum(x[i] * sizes[cands[i][0]] for i in idx) <= max(0, cap[key])
    available = pulp.listSolvers(onlyAvailable=True)
    name = next((n for n in ("HiGHS", "PULP_CBC_CMD", "COIN_CMD") if n in available), None)
    if name is None:
        raise RuntimeError("no MILP solver for the PuLP fallback: pip install highspy")
    m.solve(pulp.getSolver(name, msg=False))
    return [i for i in range(len(cands)) if (x[i].value() or 0) > 0.5]


def solve(groups: list[dict], flights: list[dict], backend: str = "auto") -> dict:
    """groups: {pnr,size,cabin,ssr,family,onward,ready,orig_std,orig_sta,booked,loyalty}
    flights: {fid,etd,eta,free_j,free_y,prm_assist,umnr_escort,next_day}"""
    if backend == "auto":
        try:
            import ortools  # noqa: F401
            backend = "cpsat"
        except ImportError:
            backend = "pulp"
    run = _solve_cpsat if backend == "cpsat" else _solve_pulp
    fl = sorted(flights, key=lambda f: (f["etd"], f["fid"]))
    cap = {(f["fid"], c): int(f["free_" + c.lower()]) for f in fl for c in "JY"}
    assignments, unassigned = [], []
    for cls, label in CLASSES:
        gs = sorted((g for g in groups if priority_class(g) == cls),
                    key=lambda g: (g["orig_std"], g["booked"], -g.get("loyalty", 0), g["pnr"]))
        if not gs:
            continue
        sizes = [g["size"] for g in gs]
        cands, why_not = [], defaultdict(set)
        for gi, g in enumerate(gs):
            for f in fl:
                reason = infeasible_reason(g, f)
                if reason:
                    why_not[gi].add(reason)
                    continue
                delay = max(0, f["eta"] - g["orig_sta"])
                cost = g["size"] * delay * 1000 + delay * (len(gs) - gi)      # total delay first, then the tie-break order
                if delay > C.LONG_DELAY_MIN:
                    cost += g["size"] * 60000
                cands.append((gi, f["fid"], g["cabin"], cost))
                if g["cabin"] == "J":                                        # downgrade allowed, never preferred
                    cands.append((gi, f["fid"], "Y", cost + g["size"] * 90000))
        chosen = {cands[i][0]: cands[i] for i in run(cands, sizes, cap)}
        by_fid = {f["fid"]: f for f in fl}
        for gi, g in enumerate(gs):
            if gi not in chosen:
                reasons = sorted(why_not[gi] - {"READY_TIME"}) or ["NO_SEATS_FOR_WHOLE_GROUP"]
                unassigned.append({"pnr": g["pnr"], "size": g["size"], "ssr": g["ssr"], "class": cls, "reasons": reasons})
                continue
            _, fid, cab, _ = chosen[gi]
            f = by_fid[fid]
            cap[(fid, cab)] -= g["size"]
            delay = max(0, f["eta"] - g["orig_sta"])
            rules = [f"R2{cls}", "R1-seats", "R1-ready-time", "R1-group-together"]
            rules += ["R1-prm-assistance"] if "WCHR" in g["ssr"] else []
            rules += ["R1-umnr-same-day-escort"] if "UMNR" in g["ssr"] else []
            rules += ["R3-downgrade"] if cab != g["cabin"] else []
            assignments.append({
                "pnr": g["pnr"], "size": g["size"], "cabin": g["cabin"], "to": fid, "to_cabin": cab, "class": cls,
                "delay_min": delay, "overnight": bool(f.get("next_day")), "rules": rules,
                "why": f"priority ({cls}) {label}; earliest flight with {g['size']} seat(s) that meets every rule is {fid} "
                       f"(dep {C.hm(f['etd'])}), arriving {delay} min later than booked"})
    return {"assignments": assignments, "unassigned": unassigned, "backend": backend,
            "stats": {"groups": len(groups), "pax": sum(g["size"] for g in groups),
                      "assigned_pax": sum(a["size"] for a in assignments),
                      "overnight_pax": sum(a["size"] for a in assignments if a["overnight"]),
                      "unassigned_pax": sum(u["size"] for u in unassigned),
                      "total_delay_pax_min": sum(a["size"] * a["delay_min"] for a in assignments)}}
