"""Stage 4a — Turnaround agent (LLM + tools).

Tracks fuelling, catering, cleaning, baggage, boarding and crew-change tasks for the next departure
of a late aircraft, predicts the off-block time and proposes ground-resource dispatch. Ground-task
coordination is the autonomous part of the system: these actions run without approval when the
guard passes (still logged and reversible).
"""
from __future__ import annotations

from .. import config as C
from ..solvers.propagation import compute, knock_on
from . import Ctx, choose, proposal

ROLE = "turnaround agent (ground tasks, off-block prediction, ground-resource dispatch)"


def run(ctx: Ctx):
    n, x = ctx.case.get("departure"), ctx.case.get("inbound")
    t = ctx.tb.get("turnaround", n) if n else None
    mv = ctx.tb.get("movements", n) if n else None
    if not t or not mv or mv["atd"] is not None or mv["status"] == "CANCELLED":
        return proposal(ctx, "turnaround", [], "No open turnaround for this case.", extras={"flight": n})
    v = ctx.tb.legs()
    legs, order = v["legs"], v["order"]
    base = compute(legs, order)
    delay = mv["dep_delay"]
    inputs = ["turnaround:" + n] + (["movements:" + x] if x else [])
    typ = legs[n]["type"]
    cands, cut, saved_so_far, k = [], 0, 0, 0
    room = t["turn_min"] - t["floor_turn"]
    ranked = sorted(C.GROUND_MEASURES.items(), key=lambda kv: (-kv[1]["saves"] / kv[1]["cost"], kv[0]))
    for name, m in ranked:
        if delay <= 0 or name in t["measures"] or t["resources"].get(name, 0) <= 0 or cut >= room:
            continue
        total = -knock_on(legs, order, {n: {"turn_cut": cut + m["saves"]}}, base)["delta_min"]
        gain = total - saved_so_far                       # network delay minutes this measure adds on top
        if gain <= 0:
            continue
        cut, saved_so_far, k = cut + m["saves"], total, k + 1
        cands.append(ctx.action(
            "turnaround", k, "GROUND_DISPATCH", n,
            f"{name.replace('_', ' ')}: {m['saves']} min off the turnaround, {gain} delay min saved down the rotation, "
            f"AED {m['cost']}",
            params={"measure": name, "saves": m["saves"], "network_min_saved": gain}, inputs=inputs,
            est_delay_delta=-gain, est_cost_delta=m["cost"] - gain * C.COST["delay_min"][typ],
            undo={"type": "GROUND_DISPATCH", "params": {"measure": name, "undo": True}}))
    if delay >= 10:
        k += 1
        cands.append(ctx.action(
            "turnaround", k, "STAFF_ALERT", n, f"Tell gate and ramp teams: {n} now expected off-block {C.hm(mv['etd'])}",
            params={"to": "gate and ramp teams", "etd": mv["etd"], "delay_min": delay}))
    default = [c.action_id for c in cands if c.type == "STAFF_ALERT" or c.est_cost_delta < 0]
    picked_cut = sum(c.params["saves"] for c in cands if c.action_id in default and c.type == "GROUND_DISPATCH")
    if delay <= 0:
        rule_text = f"{n} is predicted off-block on time ({C.hm(mv['etd'])}); no ground action needed."
    elif picked_cut:
        rule_text = (f"{n} is predicted off-block {C.hm(mv['etd'])}, {delay} min late, because the aircraft arrives late. "
                     f"Dispatching extra ground teams cuts the turnaround by {picked_cut} min and saves about "
                     f"{saved_so_far} delay minutes across the following legs.")
    else:
        rule_text = (f"{n} is predicted off-block {C.hm(mv['etd'])}, {delay} min late. No ground measure is worth its "
                     f"cost here (turnaround already at {t['turn_min']} min, floor {t['floor_turn']} min).")
    facts = {"flight": n, "inbound": x, "std": C.hm(mv["std"]), "predicted_offblock": C.hm(mv["etd"]),
             "delay_min": delay, "turn_min": t["turn_min"], "floor_turn_min": t["floor_turn"],
             "tasks": [{"task": k_["task"], "status": k_["status"]} for k_ in t["tasks"]],
             "teams_free_this_half_hour": t["resources"], "notes": t["notes"],
             "knock_on_min_without_action": ctx.extras("propagation").get("knock_on_min")}
    actions, rationale, used, warn = choose(ctx, "turnaround", ROLE, facts, cands, default, rule_text)
    return proposal(ctx, "turnaround", actions, rationale, used, warn, extras={
        "flight": n, "predicted_offblock": mv["etd"], "delay_min": delay, "tasks": t["tasks"], "notes": t["notes"],
        "candidates": [c.model_dump() for c in cands]},
        assumptions=["Task durations scale with the minimum turnaround of the aircraft type."])
