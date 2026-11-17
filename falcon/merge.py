"""Stage 5 — merge and resolve (code).

Combines every agent's proposal into one recovery plan, drops duplicates and already-decided actions,
detects collisions (same stand, same crew, same aircraft, same seat) and scores the trade-offs with the
cost model in config.py so the controller sees WHY one option is recommended over another.
"""
from __future__ import annotations

from collections import defaultdict

from . import config as C
from .models import Action

ORDER = {"GROUND": 0, "SCHEDULE": 1, "AIRCRAFT": 2, "CREW": 3, "SAFETY": 4, "PASSENGER": 5}
TYPE_ORDER = {"MASS_REACCOMMODATION": 0, "REBOOK_GROUP": 1, "REROUTE_BAGS": 1, "MEAL_VOUCHER": 3, "BOOK_HOTEL": 4,
              "SEND_MESSAGE": 5}


def _resource(a: Action) -> str | None:
    """Exclusive resource an action would take."""
    if a.type == "SWAP_CREW":
        return f"crew:{a.params.get('crew_in')}"
    if a.type == "SWAP_AIRCRAFT":
        return f"aircraft:{a.params.get('to_ac')}"
    if a.type == "REASSIGN_STAND":
        return f"stand:{a.params.get('to')}"
    return None


def merge(proposals: list[dict], case: dict, reserved: dict | None = None, seat_free=None) -> dict:
    """reserved: exclusive resources already held by other cases. seat_free(fid, cabin) -> live free seats."""
    reserved = reserved or {}
    decisions = case.get("decisions", {})
    actions = [Action.model_validate(a) for p in sorted(proposals, key=lambda p: p["agent"]) for a in p["actions"]]
    kept, dropped, collisions, seen = [], [], [], set()

    def drop(a: Action, reason: str, collision: bool = False) -> None:
        dropped.append({"action_id": a.action_id, "type": a.type, "target": a.target, "reason": reason})
        if collision:
            collisions.append({"action_id": a.action_id, "resource": _resource(a), "reason": reason})

    taken: dict[str, str] = {}          # exclusive resource -> action id holding it in this plan
    groups_done: set[str] = set()       # option groups that already have their chosen option
    # inside an option group an option a human already approved wins, then the agent's own ranking
    for a in sorted(actions, key=lambda a: (a.params.get("option_group", ""), decisions.get(a.signature()) != "APPROVED",
                                            a.params.get("rank", 0), a.action_id)):
        sig = a.signature()
        if sig in seen:
            drop(a, "duplicate of another proposal")
            continue
        state = decisions.get(sig)
        if state in ("REJECTED", "TIMEOUT", "EXECUTED"):
            drop(a, {"REJECTED": "rejected by the approver earlier in this case",
                     "TIMEOUT": "approval timed out: safe default keeps the current plan (escalated)",
                     "EXECUTED": "already executed in this case"}[state])
            continue
        group = a.params.get("option_group")
        if group and group in groups_done:
            drop(a, f"alternative option for {group} (a better-ranked option was kept)")
            continue
        res = _resource(a)
        if res and reserved.get(res) not in (None, case["case_id"]):
            drop(a, f"{res} is already held by case {reserved[res]}", collision=True)
            continue
        if res and res in taken:
            drop(a, f"{res} is already used by {taken[res]} in this plan", collision=True)
            continue
        if res:
            taken[res] = a.action_id
        if group:
            groups_done.add(group)
        seen.add(sig)
        kept.append(a)

    # same seat: never plan more groups onto a flight/cabin than it has free seats (lowest priority goes back)
    if seat_free is not None:
        need = defaultdict(list)
        for a in kept:
            if a.type == "REBOOK_GROUP":
                need[(a.params["to"], a.params.get("to_cabin", "Y"))].append(a)
        for (fid, cab), acts in need.items():
            free = seat_free(fid, cab)
            acts.sort(key=lambda a: (a.params.get("class", "e"), a.params.get("delay_min", 0), a.action_id))
            used = 0
            for a in acts:
                if used + a.params["size"] > free:
                    kept.remove(a)
                    drop(a, f"seat collision on {fid} {cab}: only {free} seat(s) left", collision=True)
                else:
                    used += a.params["size"]
    gone = {d["action_id"] for d in dropped}
    changed = True
    while changed:                       # children of a dropped action go with it
        changed = False
        for a in list(kept):
            if a.requires and a.requires in gone:
                kept.remove(a)
                gone.add(a.action_id)
                dropped.append({"action_id": a.action_id, "type": a.type, "target": a.target,
                                "reason": f"depends on dropped action {a.requires}"})
                changed = True
    def order(a: Action):                # parents before children; a bundle (one saga) stays together
        if a.bundle:
            return (ORDER["PASSENGER"], 1, a.bundle, a.type != "REBOOK_GROUP", a.action_id)
        return (ORDER[a.category], TYPE_ORDER.get(a.type, 2) if a.category == "PASSENGER" else 0, "", False, a.action_id)

    kept.sort(key=order)
    return {"actions": [a.model_dump() for a in kept], "dropped": dropped, "collisions": collisions,
            "tradeoffs": tradeoffs(kept, actions, proposals), "score": {
                "est_delay_delta_min": round(sum(a.est_delay_delta for a in kept)),
                "est_cost_delta_aed": round(sum(a.est_cost_delta for a in kept)),
                "pax_protected": sum(a.params.get("protects_pax", 0) for a in kept if a.type == "HOLD_DEPARTURE"),
                "pax_rebooked": sum(a.params.get("size", 0) for a in kept if a.type == "REBOOK_GROUP")}}


def tradeoffs(kept: list[Action], all_actions: list[Action], proposals: list[dict]) -> list[dict]:
    """One row per real decision: the options, what each costs, and which one is recommended."""
    chosen = {a.action_id for a in kept}
    rows = []
    for a in all_actions:
        p = a.params
        if a.type == "HOLD_DEPARTURE":
            rows.append({
                "decision": f"{a.target}: hold {p['minutes']} min or go on time?",
                "priority_conflict": "on-time departure vs protecting connections",
                "options": [
                    {"option": f"Hold {p['minutes']} min", "cost_aed": p.get("cost_hold"), "delay_min": p.get("knock_on_min"),
                     "pax_protected": p.get("protects_pax"), "pax_overnight": 0},
                    {"option": "Go on time", "cost_aed": p.get("cost_go"), "delay_min": 0, "pax_protected": 0,
                     "pax_stranded": p.get("protects_pax"), "pax_overnight": p.get("overnight_if_go")}],
                "recommended": f"Hold {p['minutes']} min" if a.action_id in chosen else "Go on time",
                "needs": C.ROLE_NAMES[C.DUTY_OPS] if p["minutes"] > C.HOLD_AUTO_MAX_MIN else "automatic"})
        elif a.type == "SWAP_AIRCRAFT" and p.get("rank", 0) == 0:
            rows.append({
                "decision": f"{a.target}: swap onto {p['to_ac']} or wait for the late aircraft?",
                "priority_conflict": "swap cost and spare cover vs delay down the rotation",
                "options": [
                    {"option": f"Swap to {p['to_ac']}", "cost_aed": C.COST["aircraft_swap"], "delay_min": -p.get("saves_min", 0)},
                    {"option": "Keep the planned aircraft", "cost_aed": 0, "delay_min": 0}],
                "recommended": f"Swap to {p['to_ac']}" if a.action_id in chosen else "Keep the planned aircraft",
                "needs": C.ROLE_NAMES[C.DUTY_OPS]})
        elif a.type == "SWAP_CREW" and p.get("rank", 0) == 0:
            rows.append({
                "decision": f"{a.target}: replace {p['crew_out']} (would exceed the duty limit)",
                "priority_conflict": "legality is not negotiable: only legal options are listed",
                "options": [{"option": f"Standby {x.params['crew_in']}", "cost_aed": C.COST["crew_callout"],
                             "delay_min": x.params.get("extra_delay_min", 0), "duty_margin_min": x.params.get("margin_min")}
                            for x in all_actions if x.type == "SWAP_CREW" and x.target == a.target
                            and x.params.get("crew_out") == p["crew_out"]],
                "recommended": f"Standby {p['crew_in']}", "needs": C.ROLE_NAMES[C.CREW_CTRL]})
    ground = [a for a in kept if a.type == "GROUND_DISPATCH"]
    if ground:
        rows.append({
            "decision": f"{ground[0].target}: compress the turnaround?",
            "priority_conflict": "ground-handler overtime vs delay down the rotation",
            "options": [{"option": " + ".join(a.params["measure"].replace("_", " ") for a in ground),
                         "cost_aed": sum(C.GROUND_MEASURES[a.params["measure"]]["cost"] for a in ground),
                         "delay_min": round(sum(a.est_delay_delta for a in ground))},
                        {"option": "Standard turnaround", "cost_aed": 0, "delay_min": 0}],
            "recommended": "Compress", "needs": "automatic"})
    return rows
