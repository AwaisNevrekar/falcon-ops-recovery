"""Stage 4d — Baggage agent (LLM + tools).

Finds transfer bags whose owners will still make the connection but whose bags will not (the bag MCT
is longer than the passenger MCT), and proposes tail-to-tail hot transfers within the vehicle capacity,
priority tags for the rest, and bag re-routing for every rebooked group.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil

from .. import config as C
from . import Ctx, choose, proposal

ROLE = "baggage agent (bag connection risk, hot transfers, priority tags)"


def run(ctx: Ctx):
    x, case = ctx.case.get("inbound"), ctx.case
    cands, rows, k = [], [], 0
    if x and case["type"] == "LATE_INBOUND":
        eta = ctx.tb.eta(x)["eta"]
        bags = ctx.tb.get("baggage", x)
        cap = ctx.tb.get("baggage", "capacity:" + x) or {"runs_free": 0, "bags_per_run": C.HOT_TRANSFER_BAGS_PER_RUN}
        by_out = defaultdict(list)
        for b in bags or []:
            if not b["hot"]:
                by_out[b["to"]].append(b)
        for y in sorted(by_out) if eta is not None else []:
            mv = ctx.tb.get("movements", y)
            if not mv or mv["atd"] is not None or mv["status"] == "CANCELLED":
                continue
            pax_ok, bag_ok = eta + C.MCT_PAX_MIN <= mv["etd"], eta + C.MCT_BAG_MIN <= mv["etd"]
            if not pax_ok or bag_ok:              # misconnecting passengers: their bags follow the rebooking
                continue
            rows.append({"out": y, "bags": sum(b["bags"] for b in by_out[y]), "etd": mv["etd"],
                         "priority": any(b["ssr"] for b in by_out[y]),
                         "hot_ok": eta + C.HOT_TRANSFER_MIN <= mv["etd"], "tagged": all(b["rush"] for b in by_out[y])})
        rows.sort(key=lambda r: (not r["priority"], -r["bags"], r["out"]))
        runs_left = cap["runs_free"]
        for r in rows:
            runs = ceil(r["bags"] / cap["bags_per_run"])
            inputs = ["movements:" + x, "baggage:" + x]
            worth = r["priority"] or r["bags"] >= C.HOT_TRANSFER_MIN_BAGS
            if r["hot_ok"] and worth and runs <= runs_left:
                runs_left -= runs
                k += 1
                cands.append(ctx.action(
                    "baggage", k, "HOT_TRANSFER_BAGS", x,
                    f"Tail-to-tail transfer of {r['bags']} bag(s) from {x} to {r['out']} (dep {C.hm(r['etd'])}); "
                    f"normal handling would miss it",
                    params={"inb": x, "out": r["out"], "bags": r["bags"], "runs": runs}, inputs=inputs,
                    est_cost_delta=-r["bags"] * C.COST["bag_misconnect"],
                    undo={"type": "HOT_TRANSFER_BAGS", "params": {"inb": x, "out": r["out"], "undo": True}}))
            elif not r["tagged"]:
                k += 1
                why = ("connection too short even for a hot transfer" if not r["hot_ok"] else
                       "too few bags to send a vehicle" if not worth else "no transfer vehicle free")
                cands.append(ctx.action(
                    "baggage", k, "PRIORITY_TAG_BAGS", x,
                    f"Priority-tag {r['bags']} bag(s) for {r['out']} for the next flight ({why})",
                    params={"inb": x, "out": r["out"], "bags": r["bags"]}, inputs=inputs,
                    undo={"type": "PRIORITY_TAG_BAGS", "params": {"inb": x, "out": r["out"], "undo": True}}))
    for a in ctx.prior_actions("reaccommodation"):          # bags follow every rebooked group (same saga)
        if a["type"] == "REBOOK_GROUP" and a["params"].get("bags"):
            k += 1
            p = a["params"]
            cands.append(ctx.action(
                "baggage", k, "REROUTE_BAGS", p["pnr"], f"Re-route {p['bags']} bag(s) of group {p['pnr']} to {p['to']}",
                params={"pnr": p["pnr"], "to": p["to"], "bags": p["bags"]}, bundle=a["bundle"], requires=a["action_id"],
                undo={"type": "REROUTE_BAGS", "params": {"pnr": p["pnr"], "to": p["from"]}}))
    hot = [c for c in cands if c.type == "HOT_TRANSFER_BAGS"]
    tags = [c for c in cands if c.type == "PRIORITY_TAG_BAGS"]
    rer = [c for c in cands if c.type == "REROUTE_BAGS"]
    parts = []
    if hot:
        parts.append(f"{sum(c.params['bags'] for c in hot)} bag(s) on {len(hot)} connection(s) from {x} would miss the "
                     f"normal {C.MCT_BAG_MIN}-min bag transfer; a hot transfer ({C.HOT_TRANSFER_MIN} min) saves them")
    if tags:
        parts.append(f"{sum(c.params['bags'] for c in tags)} bag(s) cannot make it and get priority tags for the next flight")
    if rer:
        parts.append(f"{sum(c.params['bags'] for c in rer)} bag(s) follow {len(rer)} rebooked group(s)")
    rule_text = ". ".join(parts) + "." if parts else "No transfer bags at risk."
    must = [c.action_id for c in rer]                       # bags always follow their passengers
    facts = {"inbound": x, "connections_with_bags_at_risk": rows,
             "bag_mct_min": C.MCT_BAG_MIN, "hot_transfer_min": C.HOT_TRANSFER_MIN, "rebooked_groups_with_bags": len(rer)}
    actions, rationale, used, warn = choose(ctx, "baggage", ROLE, facts, cands, [c.action_id for c in cands], rule_text, must)
    return proposal(ctx, "baggage", actions, rationale, used, warn, extras={
        "at_risk": rows, "bags_at_risk": sum(r["bags"] for r in rows), "hot_transfers": len(hot), "tags": len(tags)})
