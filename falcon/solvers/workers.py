"""Stage 4b workers: thin wrappers that feed the deterministic solvers from the feeds and return Proposals.

No LLM here. Rationales are templates filled with solver output.
"""
from __future__ import annotations

from collections import defaultdict

from .. import config as C
from ..agents import Ctx, proposal
from ..models import signoff_valid
from . import crew_legality, reaccommodation
from . import stands as stand_solver
from .propagation import chain, compute


def propagation(ctx: Ctx):
    v = ctx.tb.legs()
    legs, order, rot = v["legs"], v["order"], v["rot"]
    x, n = ctx.case.get("inbound"), ctx.case.get("departure")
    base = compute(legs, order)
    rows = [{"fid": f, "kind": legs[f]["kind"], "to": legs[f]["dest"], "std": legs[f]["std"], "etd": base[f][0],
             "delay": max(0, base[f][0] - legs[f]["std"]), "arr_delay": max(0, base[f][1] - legs[f]["sta"])}
            for f in chain(legs, rot, n or x) if f in base][:8]
    eta = ctx.tb.eta(x) if x else None
    knock = sum(r["delay"] for r in rows)
    late = [r for r in rows if r["delay"] > 0]
    text = (f"Inbound {x} is estimated {C.hm(eta['eta'])} ({eta['basis']}). " if eta and eta["eta"] else "")
    text += (f"Without action {len(late)} more departure(s) of this aircraft leave late, {knock} delay minutes in total."
             if late else "No knock-on delay is expected on this rotation.")
    return proposal(ctx, "propagation", [], text, extras={
        "chain": rows, "knock_on_min": knock, "next_departure": n,
        "dep_delay": rows[0]["delay"] if n and rows else 0, "inbound_eta": eta})


def crew_legality_worker(ctx: Ctx):
    n = ctx.case.get("departure")
    mv = ctx.tb.get("movements", n) if n else None
    crew = ctx.tb.get("crew", n) if n else None
    if not n or mv is None or mv["atd"] is not None or mv["status"] == "CANCELLED":
        return proposal(ctx, "crew_legality", [], "No open departure to check.", extras={"flight": n, "illegal": []})
    if crew is None:
        return proposal(ctx, "crew_legality", [], f"Crew data for {n} is unavailable: legality cannot be confirmed.",
                        extras={"flight": n, "illegal": [], "unknown": True}, confidence=0.3)
    now = ctx.tb.now
    status = [crew_legality.check(c, c["times"], now) for c in crew]
    illegal = [s for s in status if not s["legal"]]
    options, not_offered = {}, 0
    if illegal:
        standby = ctx.tb.get("crew", "standby") or []
        for s in illegal:
            member = next(c for c in crew if c["id"] == s["crew"])
            res = crew_legality.legal_options(s["role"], member["times"], standby, now)
            options[s["crew"]] = res["options"]            # legal people only; the others are not even named
            not_offered += len(res["rejected"])
    margin = min(s["margin_min"] for s in status) if status else None
    text = (f"Crew of {n} stay legal with {margin} min of duty margin." if not illegal else
            f"{', '.join(s['crew'] for s in illegal)} on {n} would exceed the duty limit ({illegal[0]['reasons'][0]}). "
            f"{sum(len(o) for o in options.values())} legal standby option(s); {not_offered} standby not offered (illegal).")
    return proposal(ctx, "crew_legality", [], text, extras={
        "flight": n, "status": status, "illegal": [s["crew"] for s in illegal], "options": options,
        "not_offered": not_offered, "min_margin": margin})


def maintenance_check(ctx: Ctx):
    n = ctx.case.get("departure")
    sched = ctx.tb.get("schedule", n) if n else None
    if not sched:
        return proposal(ctx, "maintenance_check", [], "No departure to check.", extras={"usable_spares": [], "blocked_spares": []})
    now = ctx.tb.now
    ac = ctx.tb.get("maintenance", sched["ac"])
    spares = ctx.tb.get("maintenance", "spares")
    if ac is None or spares is None:
        return proposal(ctx, "maintenance_check", [], "Maintenance feed unavailable: every aircraft is treated as "
                        "unavailable until sign-off can be read.", confidence=0.3,
                        extras={"flight": n, "aircraft": sched["ac"], "aircraft_ok": False, "unknown": True,
                                "usable_spares": [], "blocked_spares": []})
    same = sorted((s for s in spares if s["type"] == sched["type"] and s["reg"] != sched["ac"]),
                  key=lambda s: (not s["spare"], s["reg"]))            # designated spares first, then idle aircraft
    usable = [s["reg"] for s in same if signoff_valid(s, now)]
    blocked = [{"reg": s["reg"], "reason": "no current maintenance sign-off"
                + (f" ({'; '.join(s['open_items'])})" if s["open_items"] else "")} for s in same if not signoff_valid(s, now)]
    ok = signoff_valid(ac, now)
    text = (f"{sched['ac']} has a current release (signed by {ac['signoff']['by']}). " if ok else
            f"{sched['ac']} has NO current maintenance sign-off and cannot be dispatched. ")
    text += f"Usable {sched['type']} spares: {', '.join(usable) or 'none'}."
    if blocked:
        text += f" Not available: {', '.join(b['reg'] for b in blocked)} (no sign-off)."
    return proposal(ctx, "maintenance_check", [], text, extras={
        "flight": n, "aircraft": sched["ac"], "type": sched["type"], "aircraft_ok": ok, "open_items": ac["open_items"],
        "usable_spares": usable, "blocked_spares": blocked})


def stands(ctx: Ctx):
    conflicts = ctx.tb.get("stands", "conflicts")
    visits, all_stands = ctx.tb.get("stands", "visits"), ctx.tb.get("stands", "stands", snapshot=False)
    if conflicts is None or visits is None or all_stands is None:
        return proposal(ctx, "stands", [], "Stand feed unavailable.", confidence=0.3)
    mine = {ctx.case.get("inbound"), ctx.case.get("departure")} - {None}
    if ctx.case["type"] != "STAND_CONFLICT":
        conflicts = [c for c in conflicts if c["a"] in mine or c["b"] in mine]
    if not conflicts:
        return proposal(ctx, "stands", [], "No stand conflict in the next "
                        f"{C.STAND_HORIZON_MIN} minutes.", extras={"conflicts": []})
    res = stand_solver.solve(visits, all_stands, conflicts)
    actions = []
    for i, mv in enumerate(res["moves"], 1):
        actions.append(ctx.action("stands", i, "REASSIGN_STAND", mv["visit"], mv["why"], inputs=["stands:conflicts"],
                                  params={"visit": mv["visit"], "from": mv["from"], "to": mv["to"], "pax": mv["pax"],
                                          "boarding": mv["boarding"], "contact": mv["contact"]},
                                  est_cost_delta=C.COST["stand_change"], undo={"type": "REASSIGN_STAND",
                                                                               "params": {"to": mv["from"]}}))
    k = len(actions)
    for u in res["unresolved"]:
        k += 1
        actions.append(ctx.action("stands", k, "ESCALATE", str(u), f"No free compatible stand found for {u}",
                                  params={"to_role": C.DUTY_OPS, "reason": "stand conflict not solvable automatically"}))
    text = (f"{len(conflicts)} double allocation(s) found. The solver moves {len(res['moves'])} aircraft that have not "
            f"arrived yet to free compatible stands; parked aircraft stay where they are.")
    return proposal(ctx, "stands", actions, text, extras={"conflicts": conflicts, "moves": res["moves"],
                                                          "unresolved": res["unresolved"]})


def _candidate_flights(ctx: Ctx, dest: str, exclude: set, cache: dict) -> list[dict]:
    if dest not in cache:
        out = []
        for f in ctx.tb.get("schedule", "to:" + dest) or []:
            sch, mv, inv = ctx.tb.get("schedule", f), ctx.tb.get("movements", f), ctx.tb.get("inventory", f)
            if not sch or not mv or not inv or mv["status"] == "CANCELLED" or mv["etd"] < ctx.tb.now + 10:
                continue
            out.append({"fid": f, "etd": mv["etd"], "eta": mv["eta"], "free_j": inv["free_j"], "free_y": inv["free_y"],
                        "prm_assist": sch["prm_assist"], "umnr_escort": sch["umnr_escort"], "next_day": sch["kind"] == "ND"})
        cache[dest] = out
    return [f for f in cache[dest] if f["fid"] not in exclude]


def reaccommodation_worker(ctx: Ctx):
    """Who needs a new flight now that the schedule option is fixed, and where the CP-SAT solver puts them."""
    case, now = ctx.case, ctx.tb.now
    x = case.get("inbound")
    todo = defaultdict(list)                  # destination -> groups
    mov_cache, sched_cache, inputs_base = {}, {}, []

    def mov(f):
        if f not in mov_cache:
            mov_cache[f] = ctx.tb.get("movements", f)
        return mov_cache[f]

    def sched(f):
        if f not in sched_cache:
            sched_cache[f] = ctx.tb.get("schedule", f)
        return sched_cache[f]

    if case["type"] == "CANCELLATION":
        source = [g for g in ctx.tb.get("pax", "out:" + case["flight"]) or [] if g["status"] == "CANCELLED"]
        inputs_base = ["movements:" + case["flight"]]
    else:
        eta = ctx.tb.eta(x)["eta"] if x else None
        source = []
        for g in (ctx.tb.get("pax", x) or []) if eta is not None else []:
            y = mov(g["out"])
            if not y:
                continue
            if g["status"] == "MISCONNECTED" or (g["status"] in ("OK", "REBOOKED") and y["atd"] is None and (
                    y["status"] == "CANCELLED" or eta + C.MCT_PAX_MIN > y["etd"])):
                source.append(g)
        inputs_base = ["movements:" + x] if x else []
    for g in source:
        orig = sched(g["orig"])
        if not orig:
            continue
        ready = now + 30
        if g["inb"]:
            e = ctx.tb.eta(g["inb"])["eta"]
            ready = max(now, (e if e is not None else now) + C.MCT_PAX_MIN) + C.REBOOK_BUFFER_MIN
        todo[orig["dest"]].append({**g, "ready": ready, "orig_std": orig["std"], "orig_sta": orig["sta"],
                                   "dest": orig["dest"]})
    assignments, unassigned, flights_cache = [], [], {}
    for dest in sorted(todo):
        exclude = {g["out"] for g in todo[dest]} | {g["orig"] for g in todo[dest] if case["type"] == "CANCELLATION"}
        res = reaccommodation.solve(todo[dest], _candidate_flights(ctx, dest, exclude, flights_cache))
        by_pnr = {g["pnr"]: g for g in todo[dest]}
        etd = {f["fid"]: f["etd"] for f in flights_cache[dest]}
        for a in res["assignments"]:
            g = by_pnr[a["pnr"]]
            assignments.append({**a, "from": g["out"], "inb": g["inb"], "bags": g["bags"], "lang": g["lang"],
                                "ssr": g["ssr"], "etd": etd[a["to"]], "wait_min": etd[a["to"]] - g["ready"], "dest": dest})
        unassigned += [{**u, "dest": dest, "from": by_pnr[u["pnr"]]["out"]} for u in res["unassigned"]]
    pax = sum(a["size"] for a in assignments)
    actions, k, parent = [], 0, ""
    if pax > C.AUTO_REBOOK_MAX_PAX:
        by_flight = defaultdict(int)
        for a in assignments:
            by_flight[a["to"]] += a["size"]
        k += 1
        mass = ctx.action(
            "reaccommodation", k, "MASS_REACCOMMODATION", case["flight"],
            f"Re-accommodate {pax} passengers of {case['flight']} by the fairness priority table",
            params={"pax": pax, "groups": len(assignments), "overnight_pax": sum(a["size"] for a in assignments if a["overnight"]),
                    "by_flight": dict(sorted(by_flight.items())),
                    "by_class": {c: sum(a["size"] for a in assignments if a["class"] == c) for c, _ in reaccommodation.CLASSES}},
            inputs=inputs_base, est_cost_delta=pax * 40.0)
        actions.append(mass)
        parent = mass.action_id
    for a in assignments:
        k += 1
        actions.append(ctx.action(
            "reaccommodation", k, "REBOOK_GROUP", a["pnr"], a["why"], bundle=f"{case['case_id']}:{a['pnr']}",
            requires=parent, inputs=inputs_base + ["inventory:" + a["to"]],
            params={kk: a[kk] for kk in ("pnr", "size", "cabin", "to", "to_cabin", "from", "inb", "delay_min", "overnight",
                                         "rules", "class", "bags")},
            est_delay_delta=0.0, est_cost_delta=0.0,
            undo={"type": "REBOOK_GROUP", "params": {"pnr": a["pnr"], "to": a["from"]}}))
    for u in unassigned:
        k += 1
        actions.append(ctx.action(
            "reaccommodation", k, "ESCALATE", u["pnr"],
            f"No rule-compliant flight for group {u['pnr']} ({', '.join(u['ssr']) or 'group of ' + str(u['size'])}): "
            f"{', '.join(u['reasons'])}",
            params={"to_role": C.CS_LEAD, "reason": ", ".join(u["reasons"]), "pnr": u["pnr"], "must": True}))
    over = sum(a["size"] for a in assignments if a["overnight"])
    text = (f"{pax} passenger(s) in {len(assignments)} group(s) need a new flight. Solved in priority order "
            f"(PRM/UMNR, last onward connection, families, tight connections, then by original departure): "
            f"{pax - over} travel today, {over} overnight" + (f", {len(unassigned)} group(s) escalated." if unassigned else ".")
            if assignments or unassigned else "No passenger needs re-accommodation.")
    return proposal(ctx, "reaccommodation", actions, text, extras={
        "assignments": assignments, "unassigned": unassigned, "pax": pax, "overnight_pax": over, "parent": parent,
        "solver": "OR-Tools CP-SAT (PuLP fallback)", "priority_table": [f"({c}) {label}" for c, label in reaccommodation.CLASSES]})


WORKERS = {"propagation": propagation, "crew_legality": crew_legality_worker, "maintenance_check": maintenance_check,
           "stands": stands, "reaccommodation": reaccommodation_worker}
