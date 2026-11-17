"""Stage 4e — Crew & aircraft agent (LLM + tools).

Explains crew and aircraft options. It NEVER creates an option itself: crew options come only from the
legality calculator (legal people only) and aircraft options only from the maintenance check (aircraft
with a current sign-off only). Everything it proposes still needs a human: every crew change goes to
the Crew Controller, every aircraft swap to the Duty Ops Controller. With no legal option it escalates.
"""
from __future__ import annotations

from .. import config as C
from ..solvers.propagation import compute, knock_on
from . import Ctx, choose, proposal

ROLE = "crew and aircraft agent (duty-clock awareness, swap options, maintenance status)"


def run(ctx: Ctx):
    case = ctx.case
    n, x = case.get("departure"), case.get("inbound")
    mv = ctx.tb.get("movements", n) if n else None
    if not n or not mv or mv["atd"] is not None or mv["status"] == "CANCELLED":
        return proposal(ctx, "crew_aircraft", [], "No open departure: no crew or aircraft decision needed.")
    cl, mx = ctx.extras("crew_legality"), ctx.extras("maintenance_check")
    now, cands, must, default, waits, k = ctx.tb.now, [], [], [], [], 0
    crew_inputs = ["crew:" + n] + (["movements:" + x] if x else [])

    # ---------------------------------------------------------------- crew (options: legal standby only)
    for cid in cl.get("illegal", []):
        st = next(s for s in cl["status"] if s["crew"] == cid)
        opts = cl.get("options", {}).get(cid, [])
        if not opts:
            k += 1
            a = ctx.action("crew_aircraft", k, "ESCALATE", n,
                           f"{cid} would exceed the duty limit on {n} ({st['reasons'][0]}) and no legal standby "
                           f"{st['role']} is available. No crew option can be offered.",
                           params={"to_role": C.CREW_CTRL, "also": C.DUTY_OPS, "reason": "no legal crew option"})
            cands.append(a)
            must.append(a.action_id)
            continue
        for rank, o in enumerate(opts[:2]):
            k += 1
            a = ctx.action(
                "crew_aircraft", k, "SWAP_CREW", n,
                f"Replace {cid} on {n} with standby {o['crew']}: legal with {o['margin_min']} min duty margin, ready "
                f"{C.hm(o['ready_at'])}" + (f", flight waits {o['extra_delay_min']} min" if o["extra_delay_min"] else "")
                + f". {cid} would be {-st['margin_min']} min over the limit.",
                params={"crew_out": cid, "crew_in": o["crew"], "role": o["role"], "ready_at": o["ready_at"],
                        "margin_min": o["margin_min"], "extra_delay_min": o["extra_delay_min"],
                        "option_group": f"crew:{n}:{cid}", "rank": rank},
                inputs=crew_inputs, est_delay_delta=o["extra_delay_min"], est_cost_delta=C.COST["crew_callout"],
                undo={"type": "SWAP_CREW", "params": {"crew_out": o["crew"], "crew_in": cid}})
            cands.append(a)
            if rank == 0:
                default.append(a.action_id)
    if cl.get("unknown"):
        k += 1
        a = ctx.action("crew_aircraft", k, "ESCALATE", n, f"Crew data for {n} is unavailable: legality cannot be confirmed.",
                       params={"to_role": C.CREW_CTRL, "reason": "crew feed unavailable"})
        cands.append(a)
        must.append(a.action_id)

    # ---------------------------------------------------------------- aircraft (options: signed-off spares only)
    usable, blocked = mx.get("usable_spares", []), mx.get("blocked_spares", [])
    tech = bool(mx) and not mx.get("aircraft_ok", True)
    v = ctx.tb.legs()
    legs, order = v["legs"], v["order"]
    base = compute(legs, order)
    inbound_delay = max(0, base[x][1] - legs[x]["sta"]) if x and x in base else 0
    saving, ready = 0, now + C.SWAP_PREP_MIN
    if tech or inbound_delay >= C.SWAP_MIN_INBOUND_DELAY:
        if x and x in base and legs[x]["ata"] is None and set(legs[n]["crew"]) & set(legs[x]["crew"]):
            ready = max(ready, base[x][1] + C.CREW_CONNECT_MIN)       # the same crew is still on the late inbound
        saving = -knock_on(legs, order, {n: {"prev": None, "not_before": ready}}, base)["delta_min"]
    worth = tech or saving >= C.SWAP_MIN_SAVING_MIN
    ac_inputs = ["movements:" + x] if x else []
    if worth and usable:
        for rank, reg in enumerate(usable[:2]):
            k += 1
            a = ctx.action(
                "crew_aircraft", k, "SWAP_AIRCRAFT", n,
                f"Swap {n} onto spare {reg} (current maintenance sign-off, ready {C.hm(ready)})"
                + (f": saves about {saving} delay min over the rest of the rotation" if saving else
                   f": {mx.get('aircraft')} has no sign-off and cannot fly"),
                params={"to_ac": reg, "from_ac": mx.get("aircraft"), "saves_min": saving, "ready_at": ready,
                        "option_group": f"aircraft:{n}", "rank": rank},
                inputs=ac_inputs + ["maintenance:" + reg], est_delay_delta=-saving,
                est_cost_delta=C.COST["aircraft_swap"] - saving * C.COST["delay_min"][legs[n]["type"]],
                undo={"type": "SWAP_AIRCRAFT", "params": {"to_ac": mx.get("aircraft")}})
            cands.append(a)
            if rank == 0 and (tech or a.est_cost_delta < 0):
                default.append(a.action_id)
    elif worth and (blocked or tech):
        k += 1
        reg = blocked[0]["reg"] if blocked else mx.get("aircraft")
        why = (f"A swap would save about {saving} delay min on {n}, but the only spare {reg} has no maintenance "
               f"sign-off ({blocked[0]['reason']}). " if blocked and not tech else
               f"{mx.get('aircraft')} has no maintenance sign-off and no signed-off spare is free: {n} cannot depart. ")
        a = ctx.action("crew_aircraft", k, "ESCALATE", n,
                       why + "The system cannot release an aircraft; a Maintenance Control decision is needed.",
                       params={"to_role": C.MAINT, "also": C.DUTY_OPS, "reason": "no aircraft with current sign-off",
                               "aircraft": reg})
        cands.append(a)
        must.append(a.action_id)
        waits.append({"kind": "signoff", "key": reg})

    swaps = [c for c in cands if c.action_id in default]
    parts = [c.why for c in swaps] + [c.why for c in cands if c.action_id in must]
    text = " ".join(parts) if parts else (
        f"Crew of {n} stay inside duty limits (margin {cl.get('min_margin')} min) and the aircraft has a current "
        f"sign-off. No crew or aircraft change is needed.")
    facts = {"flight": n, "inbound": x, "inbound_delay_min": inbound_delay, "crew_status": cl.get("status"),
             "legal_standby_options": cl.get("options"), "aircraft": mx.get("aircraft"),
             "aircraft_signoff_current": mx.get("aircraft_ok"), "usable_spares": usable, "spares_without_signoff": blocked,
             "swap_would_save_min": saving,
             "approvals": "every crew change: Crew Controller; every aircraft swap: Duty Ops Controller"}
    actions, rationale, used, warn = choose(ctx, "crew_aircraft", ROLE, facts, cands, default, text, must)
    return proposal(ctx, "crew_aircraft", actions, rationale, used, warn,
                    extras={"waits": waits, "swap_saving_min": saving, "candidates": [c.model_dump() for c in cands]},
                    assumptions=[f"Standby call-out {C.STANDBY_CALLOUT_MIN} min, spare preparation {C.SWAP_PREP_MIN} min."])
