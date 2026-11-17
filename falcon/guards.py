"""Guard (stage 6): hard rules in plain code. NEVER an LLM.

The same rules run twice: on the merged plan (stage 6) and again inside the write gateway right before
every single write (stage 8, on live data). A failed rule removes the action. It can be escalated to a
human, but it can never be "approved away".
"""
from __future__ import annotations

from pydantic import ValidationError

from . import config as C
from .feeds.validate import reconcile_eta
from .models import ACTION_TYPES, Action, GuardResult, signoff_valid
from .solvers import crew_legality

RULES = {
    "G0": "Only known action types and measures; the category is fixed by the type",
    "G1": "Crew duty limit, minimum rest and maximum sectors",
    "G2": "No aircraft without a current maintenance sign-off",
    "G3": "Stand fits the aircraft type; no double stand allocation",
    "G4": "Seat capacity never exceeded (inventory re-read)",
    "G5": "Minimum connection time for passengers and bags",
    "G6": "PRM only with assistance; unaccompanied minors only same-day with an escort",
    "G7": "Voucher and hotel caps",
    "G8": "No action on stale, missing, conflicting or unavailable data",
    "G9": "Crew, aircraft and schedule changes need the right human approval record",
    "DEP": "Depends on an action that was removed",
}
G8_EXEMPT = {"ESCALATE", "STAFF_ALERT"}      # telling a human is always allowed


def approval_role(a: Action) -> str | None:
    """Who must sign off this action before it may run (None = autonomous, still logged and reversible)."""
    if a.type == "SWAP_CREW":
        return C.CREW_CTRL
    if a.type in ("SWAP_AIRCRAFT", "CANCEL_FLIGHT", "RETIME_DEPARTURE"):
        return C.DUTY_OPS
    if a.type == "HOLD_DEPARTURE":
        return C.DUTY_OPS if int(a.params.get("minutes", 0)) > C.HOLD_AUTO_MAX_MIN else None
    if a.type == "MASS_REACCOMMODATION":
        return C.CS_LEAD if int(a.params.get("pax", 0)) > C.AUTO_REBOOK_MAX_PAX else None
    if a.type == "BOOK_HOTEL":
        return C.CS_LEAD if float(a.params.get("total_cost", 0)) > C.HOTEL_AUTO_BUDGET_AED else None
    if a.type == "REASSIGN_STAND":
        return C.DUTY_OPS if a.params.get("boarding") else None
    return None


def safe_action(raw: dict) -> tuple[Action | None, str]:
    """Build an Action from untrusted input (an LLM, a test). Unknown types are rejected (G0)."""
    try:
        return Action.model_validate(raw), ""
    except ValidationError as e:
        return None, f"G0: {e.errors()[0]['msg']}"


class GuardCtx:
    """Live data for the rules. `approved(action)` returns the approval row or None; `executed(id)` a bool."""

    def __init__(self, hub, approved=None, executed=None):
        self.hub = hub
        self.approved = approved or (lambda action: None)
        self.executed = executed or (lambda action_id: False)

    @property
    def now(self) -> int:
        return self.hub.world.now

    def val(self, feed: str, key: str):
        r = self.hub.get(feed, key, snapshot=False)
        return r.value if r.status == "OK" else None

    def eta(self, fid: str) -> tuple[int | None, str]:
        rec = reconcile_eta(self.hub.get("movements", fid, snapshot=False), self.hub.get("atc", fid, snapshot=False))
        return rec["eta"], rec["status"]

    def input_status(self, ref: str) -> str:
        feed, _, key = ref.partition(":")
        r = self.hub.get(feed, key or "*", snapshot=False)
        if feed == "movements" and r.status == "OK" and isinstance(r.value, dict) \
                and r.value.get("kind") == "ARR" and r.value.get("ata") is None:
            return self.eta(key)[1]                 # also catches an airline-vs-ATC conflict
        return r.status


def check_action(a: Action, plan: list[Action], ctx: GuardCtx, stage: str = "plan") -> list[GuardResult]:
    res: list[GuardResult] = []
    p, now = a.params, ctx.now

    def add(rule: str, ok, detail: str = "") -> None:
        res.append(GuardResult(action_id=a.action_id, rule=rule, passed=bool(ok), detail=detail))

    add("G0", a.type in ACTION_TYPES and a.category == ACTION_TYPES.get(a.type),     # ---- G0 known type, fixed category
        f"{a.type} is a known {a.category} action" if a.type in ACTION_TYPES else f"unknown action type {a.type}")
    if a.type not in G8_EXEMPT:                                                     # ---- G8 data quality
        bad = [(r, s) for r in a.inputs for s in [ctx.input_status(r)] if s != "OK"]
        add("G8", not bad, "; ".join(f"{r} is {s}" for r, s in bad) or f"{len(a.inputs)} input(s) fresh and consistent")

    if a.type == "GROUND_DISPATCH":
        add("G0", p.get("measure") in C.GROUND_MEASURES, f"measure {p.get('measure')}")

    if a.type == "SWAP_CREW":                                                       # ---- G1 crew
        crew, standby = ctx.val("crew", a.target), ctx.val("crew", "standby")
        out = next((c for c in crew or [] if c["id"] == p.get("crew_out")), None)
        inn = next((c for c in standby or [] if c["id"] == p.get("crew_in")), None)
        if crew is None or standby is None:
            add("G1", False, "crew data unavailable - legality cannot be verified")
        elif not out or not inn:
            add("G1", False, "crew is not on this flight / not an available standby")
        else:
            opt = crew_legality.legal_options(out["role"], out["times"], [inn], now)
            add("G1", opt["options"], f"{inn['id']} legal, margin {opt['options'][0]['margin_min']} min"
                if opt["options"] else "; ".join(opt["rejected"][0]["reasons"]) if opt["rejected"] else "wrong role")

    if a.type == "HOLD_DEPARTURE":
        minutes = int(p.get("minutes", 0))
        crew = ctx.val("crew", a.target)
        if crew is None:
            add("G1", False, "crew data unavailable - legality cannot be verified")
        else:
            over = [r["reasons"][0] for c in crew
                    for r in [crew_legality.check(c, [(f, d + minutes, e + minutes) for f, d, e in c["times"]], now)]
                    if not r["legal"]]
            add("G1", not over, "; ".join(over) or "crew stay inside duty limits with the hold")
        add("G9", 0 < minutes <= C.HOLD_MAX_MIN, f"hold {minutes} min (maximum {C.HOLD_MAX_MIN})")

    if a.type == "SWAP_AIRCRAFT":                                                   # ---- G2 aircraft
        ac, sched = ctx.val("maintenance", str(p.get("to_ac", ""))), ctx.val("schedule", a.target)
        if ac is None or sched is None:
            add("G2", False, "maintenance record unavailable - aircraft treated as unavailable")
        else:
            ok = signoff_valid(ac, now) and ac["free_at_hub"] and ac["type"] == sched["type"]
            why = ("current sign-off by " + ac["signoff"]["by"]) if signoff_valid(ac, now) else "NO current maintenance sign-off"
            add("G2", ok, f"{ac['reg']}: {why}; type {ac['type']}; free at hub: {ac['free_at_hub']}")

    if a.type == "REASSIGN_STAND":                                                  # ---- G3 stands
        stands, visits = ctx.val("stands", "stands"), ctx.val("stands", "visits")
        v = next((x for x in visits or [] if x["id"] == p.get("visit", a.target)), None)
        s = (stands or {}).get(p.get("to"))
        if not v or not s:
            add("G3", False, "unknown stand or ground visit")
        else:
            by_id = {x["id"]: x for x in visits}
            moving = {x.params.get("visit", x.target) for x in plan if x.type == "REASSIGN_STAND"} | {v["id"]}
            there = [w for w in visits if w["stand"] == p["to"] and w["id"] not in moving]
            there += [by_id[x.params.get("visit", x.target)] for x in plan if x.type == "REASSIGN_STAND" and x is not a
                      and x.params.get("to") == p["to"] and x.params.get("visit", x.target) in by_id]
            clash = [w["id"] for w in there if v["start"] < w["end"] and w["start"] < v["end"]]
            add("G3", v["type"] in s["types"] and not clash,
                f"{p['to']} takes {'/'.join(s['types'])}, aircraft is {v['type']}; overlapping visits: {clash or 'none'}")

    if a.type == "REBOOK_GROUP":                                                    # ---- G4 G5 G6 passengers
        to, cab, size = p.get("to", ""), p.get("to_cabin", p.get("cabin", "Y")), int(p.get("size", 1))
        inv = ctx.val("inventory", to)
        need = size + sum(int(x.params.get("size", 1)) for x in plan if x is not a and x.type == "REBOOK_GROUP"
                          and x.params.get("to") == to and x.params.get("to_cabin", x.params.get("cabin", "Y")) == cab)
        free = inv["free_" + cab.lower()] if inv else None
        add("G4", inv is not None and need <= free, f"{need} seat(s) needed in {cab} on {to}, {free} free")
        mov = ctx.val("movements", to) if not to.startswith("ND-") else ctx.val("schedule", to)
        etd = (mov.get("etd") or mov.get("std")) if mov else None
        ready = now
        if p.get("inb"):
            eta, _ = ctx.eta(p["inb"])
            ready = max(now, eta + C.MCT_PAX_MIN) if eta is not None else None
        add("G5", etd is not None and ready is not None and etd >= ready and (mov or {}).get("status") != "CANCELLED",
            f"{to} leaves {C.hm(etd)}, passengers ready {C.hm(ready)} (MCT {C.MCT_PAX_MIN} min)")
        grp, sched = ctx.val("pax", "pnr:" + str(p.get("pnr"))), ctx.val("schedule", to)
        if not grp or sched is None:
            add("G6", False, "passenger or flight record unavailable")
        else:
            ssr = grp[0]["ssr"]
            ok = ("WCHR" not in ssr or sched["prm_assist"]) and \
                 ("UMNR" not in ssr or (sched["kind"] != "ND" and sched["umnr_escort"]))
            add("G6", ok, f"SSR {ssr or 'none'}; assistance {sched['prm_assist']}, escort {sched['umnr_escort']}, "
                          f"{'next day' if sched['kind'] == 'ND' else 'same day'}")

    if a.type == "HOT_TRANSFER_BAGS":
        eta, _ = ctx.eta(str(p.get("inb")))
        out = ctx.val("movements", str(p.get("out")))
        ok = eta is not None and out is not None and out["atd"] is None and eta + C.HOT_TRANSFER_MIN <= out["etd"]
        add("G5", ok, f"bags in {C.hm(eta)} + {C.HOT_TRANSFER_MIN} min vs departure {C.hm(out['etd'] if out else None)}")

    if a.type == "MEAL_VOUCHER":                                                    # ---- G7 money caps
        amt = float(p.get("amount_per_pax", 0))
        add("G7", 0 < amt <= C.VOUCHER_MAX_AED, f"AED {amt:.0f} per passenger (cap {C.VOUCHER_MAX_AED})")
    if a.type == "BOOK_HOTEL":
        rate = float(p.get("rate", 0))
        rooms, pax = int(p.get("rooms", 0)), int(p.get("pax", 0))
        add("G7", 0 < rate <= C.HOTEL_MAX_RATE_AED and 0 < rooms <= max(pax, 1),
            f"{rooms} room(s) at AED {rate:.0f} (cap {C.HOTEL_MAX_RATE_AED}) for {pax} passenger(s)")

    role = approval_role(a)                                                         # ---- G9 human approval
    if role and stage == "execute":
        row = ctx.approved(a) or {}
        ok = (row.get("status") == "APPROVED" and bool(row.get("decided_by"))
              and row.get("role") in (role, C.ESCALATION_ROLE.get(role)))
        add("G9", ok, f"approved by {row.get('decided_by')} ({row.get('role')})" if ok
            else f"no approval record from {C.ROLE_NAMES[role]}")
    elif role:
        add("G9", True, f"will wait for {C.ROLE_NAMES[role]}")
    if a.requires and stage == "execute":
        add("G9", ctx.executed(a.requires), f"parent action {a.requires} approved and executed")
    return res


def run_guards(plan: list[Action], ctx: GuardCtx, stage: str = "plan") -> dict:
    """Check every action. Returns kept actions, removed actions (with the failed rules) and all results."""
    results, removed, gone = [], [], set()
    for a in plan:
        rs = check_action(a, plan, ctx, stage)
        results += rs
        fails = [r for r in rs if not r.passed]
        if fails:
            gone.add(a.action_id)
            removed.append((a, fails))
    changed = True
    while changed:                                   # anything that depended on a removed action goes too
        changed = False
        for a in plan:
            if a.action_id not in gone and a.requires in gone:
                r = GuardResult(action_id=a.action_id, rule="DEP", passed=False, detail=f"depends on removed {a.requires}")
                results.append(r)
                removed.append((a, [r]))
                gone.add(a.action_id)
                changed = True
    return {"kept": [a for a in plan if a.action_id not in gone], "removed": removed, "results": results}
