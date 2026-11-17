"""Stage 4c — Passenger agent (LLM + tools).

Phase A (schedule still open): finds connections at risk on a late inbound and proposes hold-vs-go
options with their costs — the trade-off between on-time departure and protecting connections.
Phase B (schedule fixed, after the re-accommodation solver): care entitlements — meal vouchers, hotel
rooms from the partner API — and passenger messages from fixed templates (English / Arabic).
Passengers appear only as hashed ids. Names and booking references never reach this agent.
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil

from .. import config as C
from ..solvers.propagation import compute, knock_on
from . import Ctx, choose, proposal

ROLE = "passenger agent (connections at risk, care entitlements, passenger messages)"
TEMPLATES = {
    "rebooked": {
        "en": "Falcon Gulf Airways: your journey has changed. You are now booked on flight {to}, departing {etd}. "
              "Your bags will travel with you. We are sorry for the disruption.",
        "ar": "طيران فالكون الخليج: تم تعديل رحلتكم. تم حجزكم على الرحلة {to} التي تغادر الساعة {etd}. "
              "ستنتقل أمتعتكم معكم. نعتذر عن الإزعاج.",
    },
    "overnight": {
        "en": "Falcon Gulf Airways: we could not find a seat for you today. You are booked on flight {to} tomorrow at "
              "{etd}. A hotel room and meals are arranged; please go to the transfer desk. We are sorry for the disruption.",
        "ar": "طيران فالكون الخليج: لم نتمكن من إيجاد مقعد لكم اليوم. تم حجزكم على الرحلة {to} غداً الساعة {etd}. "
              "تم ترتيب غرفة فندقية ووجبات؛ يرجى التوجه إلى مكتب التحويل. نعتذر عن الإزعاج.",
    },
}


def render(template: str, lang: str, **values) -> str:
    """Passenger message from a fixed template. Every value comes from code, none from an LLM."""
    t = TEMPLATES[template]
    return t.get(lang, t["en"]).format(**values)


def run(ctx: Ctx):
    if ctx.case["type"] == "LATE_INBOUND" and ctx.case["phase"] == "A":
        return _holds(ctx)
    return _care(ctx)


def _holds(ctx: Ctx):
    x = ctx.case["inbound"]
    eta = ctx.tb.eta(x)["eta"]
    groups = ctx.tb.get("pax", x)
    if eta is None or groups is None:
        return proposal(ctx, "passenger", [], f"Connections from {x} cannot be assessed: ETA or passenger data missing.",
                        confidence=0.3, extras={"connections": []})
    by_out = defaultdict(list)
    for g in groups:
        if g["status"] == "OK":
            by_out[g["out"]].append(g)
    v = ctx.tb.legs()
    legs, order = v["legs"], v["order"]
    base = compute(legs, order)
    cands, table, k = [], [], 0
    for y in sorted(by_out):
        mv = ctx.tb.get("movements", y)
        if not mv or mv["atd"] is not None or mv["status"] == "CANCELLED":
            continue
        need = eta + C.MCT_PAX_MIN - mv["etd"]
        if need <= 0:
            continue
        pax = sum(g["size"] for g in by_out[y])
        row = {"flight": y, "etd": mv["etd"], "pax_at_risk": pax, "hold_needed_min": need,
               "prm_umnr_groups": sum(1 for g in by_out[y] if g["ssr"])}
        table.append(row)
        if need > C.HOLD_MAX_MIN:
            row["decision"] = f"needs {need} min, above the {C.HOLD_MAX_MIN}-min maximum hold: rebook"
            continue
        inv, sch = ctx.tb.get("inventory", y), ctx.tb.get("schedule", y)
        if not inv or not sch:
            continue
        onboard = max(0, sch["seats_j"] + sch["seats_y"] - inv["free_j"] - inv["free_y"] - pax)
        ko = knock_on(legs, order, {y: {"hold": need}}, base)["delta_min"]
        later = 0
        for f in ctx.tb.get("schedule", "to:" + sch["dest"]) or []:
            if f == y or f.startswith("ND-"):
                continue
            fm, fi = ctx.tb.get("movements", f), ctx.tb.get("inventory", f)
            if fm and fi and fm["etd"] > mv["etd"]:
                later += fi["free_y"] + fi["free_j"]
        overnight = max(0, pax - later)
        cost_hold = ko * C.COST["delay_min"][sch["type"]] + onboard * need * C.COST["pax_delay_min"]
        cost_go = pax * C.COST["misconnect_pax"] + overnight * C.COST["overnight_pax"]
        row.update(cost_hold=round(cost_hold), cost_go=round(cost_go), knock_on_min=ko, overnight_if_go=overnight,
                   onboard=onboard, decision="hold" if cost_hold < cost_go else "go")
        k += 1
        cands.append(ctx.action(
            "passenger", k, "HOLD_DEPARTURE", y,
            f"Hold {y} for {need} min so {pax} connecting passenger(s) from {x} make it. Holding costs about "
            f"AED {cost_hold:,.0f} ({ko} delay min, {onboard} on board wait); going on time strands them, about "
            f"AED {cost_go:,.0f}" + (f" ({overnight} would stay overnight)" if overnight else ""),
            params={"minutes": need, "inbound": x, "protects_pax": pax, "knock_on_min": ko, "onboard": onboard,
                    "cost_hold": round(cost_hold), "cost_go": round(cost_go), "overnight_if_go": overnight},
            inputs=["movements:" + x, "movements:" + y, "pax:" + x],
            est_delay_delta=ko, est_cost_delta=cost_hold - cost_go,
            undo={"type": "HOLD_DEPARTURE", "params": {"minutes": -need}}))
    default = [c.action_id for c in cands if c.est_cost_delta < 0]
    at_risk = sum(r["pax_at_risk"] for r in table)
    held = [c for c in cands if c.action_id in default]
    if not table:
        text = f"No connection from {x} is at risk."
    else:
        text = (f"{at_risk} passenger(s) on {len(table)} onward flight(s) would miss their connection from {x}. "
                + (f"Holding {', '.join(c.target for c in held)} protects {sum(c.params['protects_pax'] for c in held)} "
                   f"of them at lower cost than rebooking. " if held else "No hold is worth its cost. ")
                + "The rest are re-accommodated once the schedule is fixed.")
    facts = {"inbound": x, "inbound_eta": C.hm(eta), "passenger_mct_min": C.MCT_PAX_MIN, "max_hold_min": C.HOLD_MAX_MIN,
             "connections_at_risk": table}
    actions, rationale, used, warn = choose(ctx, "passenger", ROLE, facts, cands, default, text)
    return proposal(ctx, "passenger", actions, rationale, used, warn, extras={
        "connections": table, "pax_at_risk": at_risk, "candidates": [c.model_dump() for c in cands]},
        assumptions=[f"Passenger minimum connection time {C.MCT_PAX_MIN} min.", "Costs from the model in config.py."])


def _care(ctx: Ctx):
    ex = ctx.extras("reaccommodation")
    assigns, parent, flight = ex.get("assignments", []), ex.get("parent", ""), ctx.case["flight"]
    cands, warn, k = [], [], 0
    meal = {a["pnr"]: a["size"] for a in assigns if a["overnight"] or a["wait_min"] >= C.MEAL_VOUCHER_AFTER_MIN}
    hotel = {a["pnr"]: ceil(a["size"] / 2) for a in assigns if a["overnight"]}
    hotel_pax = {a["pnr"]: a["size"] for a in assigns if a["overnight"]}
    # Groups rebooked overnight earlier in this case that still have no room (e.g. the hotel API was down).
    key = "orig:" + flight if ctx.case["type"] == "CANCELLATION" else ctx.case.get("inbound") or ""
    for g in (ctx.tb.get("pax", key) or []) if key else []:
        if g["status"] == "REBOOKED" and g["overnight"] and not g["hotel"]:
            hotel.setdefault(g["pnr"], ceil(g["size"] / 2))
            hotel_pax.setdefault(g["pnr"], g["size"])
    if meal:
        k += 1
        n = sum(meal.values())
        cands.append(ctx.action(
            "passenger", k, "MEAL_VOUCHER", flight,
            f"Meal vouchers of AED {C.MEAL_VOUCHER_AED} for {n} passenger(s) waiting over "
            f"{C.MEAL_VOUCHER_AFTER_MIN // 60} h or overnight",
            params={"pnrs": meal, "pax": n, "amount_per_pax": C.MEAL_VOUCHER_AED, "total": n * C.MEAL_VOUCHER_AED},
            requires=parent, est_cost_delta=n * C.MEAL_VOUCHER_AED, undo={"type": "MEAL_VOUCHER", "params": {"void": True}}))
    if hotel:
        k += 1
        rooms, pax = sum(hotel.values()), sum(hotel_pax.values())
        h = ctx.tb.read("hotel", "availability")
        if h.status != "OK":
            warn.append("Hotel partner API is not answering: the hotel booking waits for the partner; nothing else waits.")
        elif h.value["rooms"] < rooms:
            warn.append(f"Hotel partner has only {h.value['rooms']} rooms for {rooms} needed.")
        cands.append(ctx.action(
            "passenger", k, "BOOK_HOTEL", flight,
            f"{rooms} hotel room(s) for {pax} passenger(s) who cannot travel today, AED {rooms * C.HOTEL_RATE_AED:,}",
            params={"rooms_by_pnr": hotel, "rooms": rooms, "pax": pax, "rate": C.HOTEL_RATE_AED,
                    "total_cost": rooms * C.HOTEL_RATE_AED},
            requires=parent, inputs=["hotel:availability"], est_cost_delta=rooms * C.HOTEL_RATE_AED,
            undo={"type": "BOOK_HOTEL", "params": {"cancel": True}}))
    if assigns:
        k += 1
        cands.append(ctx.action(
            "passenger", k, "SEND_MESSAGE", flight,
            f"Message {len(assigns)} rebooked group(s) in their own language (template, English/Arabic)",
            params={"pnrs": {a["pnr"]: {"lang": a["lang"], "to": a["to"], "etd": a["etd"],
                                        "template": "overnight" if a["overnight"] else "rebooked"} for a in assigns},
                    "count": len(assigns)},
            requires=parent, undo={"type": "SEND_MESSAGE", "params": {"retract": True}}))
    pax = sum(a["size"] for a in assigns)
    text = (f"{pax} rebooked passenger(s): {sum(meal.values())} get a meal voucher, {sum(hotel.values())} hotel room(s) "
            f"for those who stay overnight, and every group gets a templated message in English or Arabic."
            if assigns else "Nobody was rebooked, so no care or messages are needed.")
    facts = {"flight": flight, "rebooked_pax": pax, "overnight_pax": ex.get("overnight_pax", 0),
             "meal_voucher_pax": sum(meal.values()), "hotel_rooms": sum(hotel.values()),
             "voucher_policy": f"AED {C.MEAL_VOUCHER_AED} after {C.MEAL_VOUCHER_AFTER_MIN} min wait",
             "hotel_budget_auto_aed": C.HOTEL_AUTO_BUDGET_AED, "notes": warn}
    actions, rationale, used, warn2 = choose(ctx, "passenger", ROLE, facts, cands, [c.action_id for c in cands], text)
    return proposal(ctx, "passenger", actions, rationale, used, warn + warn2, extras={
        "meal_pax": sum(meal.values()), "hotel_rooms": sum(hotel.values()), "messages": len(assigns)})
