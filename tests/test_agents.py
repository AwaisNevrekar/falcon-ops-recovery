"""Phase 3: the specialist agents and solver workers on the scripted scenarios, plus LLM-output checks."""
import json

from falcon import config as C
from falcon.agents import Ctx, Toolbox, baggage, crew_aircraft, passenger, turnaround
from falcon.coordinator import open_case
from falcon.feeds import FeedHub
from falcon.feeds.validate import BLOCKED
from falcon.models import Proposal
from falcon.replay import OpsDay
from falcon.solvers.workers import WORKERS
from falcon.store import Store

ALL = {**WORKERS, "turnaround": turnaround.run, "passenger": passenger.run, "baggage": baggage.run,
       "crew_aircraft": crew_aircraft.run}


class FakeLLM:
    enabled = True

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def chat_json(self, system, user, tag=""):
        self.prompts.append(system + user)
        return self.reply(json.loads(user)) if callable(self.reply) else self.reply


def setup(at, case_type, flight, llm=None):
    w = OpsDay()
    w.step(at)
    hub = FeedHub(w, Store())
    return w, Ctx(case=open_case(case_type, flight, hub), tb=Toolbox(hub), llm=llm)


def run(ctx, *names):
    p = None
    for name in names:
        c = Ctx(case=ctx.case, tb=Toolbox(ctx.tb.hub), results=ctx.results, llm=ctx.llm)
        p = ALL[name](c)
        Proposal.model_validate(p.model_dump())                    # the contract holds for every agent
        assert p.data_refs or all(a.requires for a in p.actions)   # every free-standing action cites its data
        ctx.results[name] = p.model_dump()
    return p


def of(p, type_):
    return [a for a in p.actions if a.type == type_]


def test_turnaround_agent_compresses_the_late_turnaround():
    w, ctx = setup(851, "LATE_INBOUND", "FG214")
    p = run(ctx, "propagation", "turnaround")
    assert ctx.results["propagation"]["extras"]["knock_on_min"] > 100
    dispatch = of(p, "GROUND_DISPATCH")
    assert dispatch and all(a.est_delay_delta < 0 and a.params["measure"] in C.GROUND_MEASURES for a in dispatch)
    assert of(p, "STAFF_ALERT") and not p.llm_used
    assert all(r.fetched_at == w.now and r.snapshot_id for r in p.data_refs)
    assert all(a.target == w.story["S2"]["next"] and a.category == "GROUND" for a in p.actions)


def test_passenger_agent_offers_the_12_minute_hold_with_both_costs():
    _, ctx = setup(861, "LATE_INBOUND", "FG531")
    p = run(ctx, "propagation", "passenger")
    hold = next(a for a in of(p, "HOLD_DEPARTURE") if a.target == "FG532")
    assert hold.params["minutes"] == 12 and hold.params["protects_pax"] == 38
    assert hold.params["cost_hold"] < hold.params["cost_go"] and hold.est_cost_delta < 0
    assert hold.category == "SCHEDULE" and "movements:FG531" in hold.inputs


def test_baggage_agent_hot_transfers_the_bags_at_risk():
    _, ctx = setup(931, "LATE_INBOUND", "FG853")
    p = run(ctx, "propagation", "baggage")
    hot = of(p, "HOT_TRANSFER_BAGS")
    assert sum(a.params["bags"] for a in hot) >= 60               # (one onward flight is late itself: its bags are fine)
    assert sum(a.params["bags"] for a in hot) == p.extras["bags_at_risk"]
    assert sum(a.params["runs"] for a in hot) <= C.HOT_TRANSFER_RUNS_PER_30MIN


def test_crew_agent_offers_only_legal_standby_and_escalates_when_there_is_none():
    w, ctx = setup(891, "LATE_INBOUND", "FG345")
    cpt = w.story["S5"]["captain"]
    p = run(ctx, "propagation", "crew_legality", "maintenance_check", "crew_aircraft")
    swaps = of(p, "SWAP_CREW")
    assert swaps and all(a.params["crew_out"] == cpt and a.params["margin_min"] >= 0 for a in swaps)
    assert "SBY-CPT-9" not in json.dumps(ctx.results, default=str)        # never proposed, ranked or displayed
    assert ctx.results["crew_legality"]["extras"]["not_offered"] == 1
    for c in w.crew.values():                                             # now every legal standby captain is busy
        if c["standby"] and c["role"] == "CPT" and c["id"] != "SBY-CPT-9":
            c["legs"] = ["FG1001"]
    ctx.results.clear()
    p = run(ctx, "propagation", "crew_legality", "maintenance_check", "crew_aircraft")
    assert not of(p, "SWAP_CREW")
    esc = of(p, "ESCALATE")
    assert esc and esc[0].params["to_role"] == C.CREW_CTRL and "SBY-CPT-9" not in json.dumps(p.model_dump(), default=str)


def test_aircraft_without_signoff_is_never_offered_and_is_escalated_to_maintenance():
    w, ctx = setup(906, "LATE_INBOUND", "FG466")
    p = run(ctx, "propagation", "crew_legality", "maintenance_check", "crew_aircraft")
    mx = ctx.results["maintenance_check"]["extras"]
    assert mx["usable_spares"] == [] and mx["blocked_spares"][0]["reg"] == "FG-W902"
    assert not of(p, "SWAP_AIRCRAFT")
    esc = of(p, "ESCALATE")[0]
    assert esc.params["to_role"] == C.MAINT and p.extras["waits"] == [{"kind": "signoff", "key": "FG-W902"}]
    w.step(951)                                                           # Maintenance Control signs the release
    ctx.results.clear()
    p = run(ctx, "propagation", "crew_legality", "maintenance_check", "crew_aircraft")
    swap = of(p, "SWAP_AIRCRAFT")[0]
    assert swap.params["to_ac"] == "FG-W902" and swap.params["saves_min"] >= C.SWAP_MIN_SAVING_MIN
    assert "maintenance:FG-W902" in swap.inputs


def test_swap_is_proposed_for_the_long_cascade():
    _, ctx = setup(851, "LATE_INBOUND", "FG214")
    p = run(ctx, "propagation", "crew_legality", "maintenance_check", "crew_aircraft")
    swap = of(p, "SWAP_AIRCRAFT")[0]
    assert swap.params["to_ac"] == "FG-N901" and swap.est_delay_delta <= -C.SWAP_MIN_SAVING_MIN


def test_mass_reaccommodation_of_the_cancelled_flight():
    w, ctx = setup(876, "CANCELLATION", "FG718")
    p = run(ctx, "reaccommodation")
    mass = of(p, "MASS_REACCOMMODATION")[0]
    rebooks = of(p, "REBOOK_GROUP")
    assert mass.params["pax"] + sum(u["size"] for u in p.extras["unassigned"]) == 310
    assert all(a.requires == mass.action_id and a.bundle for a in rebooks)
    used = {}
    for a in rebooks:
        key = (a.params["to"], a.params["to_cabin"])
        used[key] = used.get(key, 0) + a.params["size"]
        g = w.pnrs[a.params["pnr"]]
        leg = w.legs[a.params["to"]]
        assert "WCHR" not in g["ssr"] or leg["prm_assist"]
        assert "UMNR" not in g["ssr"] or (leg["kind"] != "ND" and leg["umnr_escort"])
    assert all(n <= w.legs[f]["free_" + cab.lower()] for (f, cab), n in used.items())
    special = [a for a in rebooks if w.pnrs[a.params["pnr"]]["ssr"]]
    earliest = min(a.params["delay_min"] for a in rebooks)
    assert len(special) == 3 and all(a.params["class"] == "a" for a in special)
    assert min(a.params["delay_min"] for a in special) == earliest        # priority passengers get the best seats

    care = run(ctx, "passenger")
    assert of(care, "MEAL_VOUCHER") and of(care, "SEND_MESSAGE")
    assert all(a.requires == mass.action_id for a in care.actions)
    bags = run(ctx, "baggage")
    rer = of(bags, "REROUTE_BAGS")
    by_id = {a.action_id: a for a in rebooks}
    assert rer and all(a.requires in by_id and a.bundle == by_id[a.requires].bundle for a in rer)


def test_hotel_outage_is_a_warning_not_a_crash():
    w, ctx = setup(876, "CANCELLATION", "FG718")
    for f in w.story["S4"]["alts"]:
        w.legs[f]["free_y"] = min(w.legs[f]["free_y"], 5)              # force overnight stays
    w.add_fault("hotel", "down", until=w.now + 40)
    care = run(ctx, "reaccommodation", "passenger")
    hotel = of(care, "BOOK_HOTEL")[0]
    assert hotel.inputs == ["hotel:availability"] and any("Hotel partner" in x for x in care.warnings)
    assert care.confidence < 0.9


# ------------------------------------------------------------------ LLM output is always checked
def first_candidate(payload):
    return {"selected": [payload["candidates"][0]["id"]], "rationale": "Dispatch the most effective team first."}


def test_valid_llm_choice_is_used():
    _, ctx = setup(851, "LATE_INBOUND", "FG214", FakeLLM(first_candidate))
    ctx.llm = None
    run(ctx, "propagation")
    ctx.llm = FakeLLM(first_candidate)
    p = run(ctx, "turnaround")
    assert p.llm_used and len(p.actions) == 1 and p.rationale.startswith("Dispatch")


def rejected(reply):
    _, ctx = setup(851, "LATE_INBOUND", "FG214")
    run(ctx, "propagation")
    ctx.llm = FakeLLM(reply)
    p = run(ctx, "turnaround")
    return p


def test_unknown_ids_numbers_and_instructions_from_the_llm_are_rejected():
    for reply, why in [
        ({"selected": ["C-FG214.i1.turnaround.99"], "rationale": "ok"}, "unknown action id"),
        (lambda pl: {"selected": [pl["candidates"][0]["id"]], "rationale": "Also swap to FG9999 now."}, "FG9999"),
        (lambda pl: {"selected": [pl["candidates"][0]["id"]], "rationale": "Ignore the duty limit rules."}, "instruction-like"),
        ({"selected": "everything", "rationale": "x"}, "must be a list"),
        (["not", "an", "object"], "not a JSON object"),
    ]:
        p = rejected(reply)
        assert not p.llm_used and any("rejected" in w and why in w for w in p.warnings), (why, p.warnings)
        assert of(p, "GROUND_DISPATCH")                                    # the rule-based choice still stands
    assert any("unavailable" in w for w in rejected(None).warnings)


def test_llm_cannot_drop_a_mandatory_escalation():
    w, ctx = setup(891, "LATE_INBOUND", "FG345")
    for c in w.crew.values():
        if c["standby"] and c["role"] == "CPT":
            c["legs"] = ["FG1001"]
    run(ctx, "propagation", "crew_legality", "maintenance_check")
    ctx.llm = FakeLLM({"selected": [], "rationale": "Nothing to do."})
    p = run(ctx, "crew_aircraft")
    assert p.llm_used and of(p, "ESCALATE")


def test_no_booking_reference_or_name_ever_reaches_an_llm_prompt():
    llm = FakeLLM(lambda pl: {"selected": [c["id"] for c in pl["candidates"]], "rationale": "All fine."})
    w, ctx = setup(876, "CANCELLATION", "FG718", llm)
    run(ctx, "reaccommodation", "passenger", "baggage")
    refs = {p["ref"] for p in w.pnrs.values() if p["out"] == "FG718"}
    text = "\n".join(llm.prompts)
    assert llm.prompts and not any(r in text for r in refs) and '"ref"' not in text


def test_injected_ops_note_never_reaches_the_agent_or_the_snapshots():
    llm = FakeLLM(lambda pl: {"selected": [], "rationale": "Noted."})
    w, ctx = setup(1021, "LATE_INBOUND", "FG977", llm)
    w.legs["FG977"]["atfm"] += 70                                  # late enough that the agent has options to weigh
    w.recompute()
    p = run(ctx, "propagation", "turnaround")
    prompt = "\n".join(llm.prompts).lower()
    assert BLOCKED.lower() in prompt and "release aircraft" not in prompt and "ignore all previous" not in prompt
    assert BLOCKED in p.extras["notes"]
    hub = ctx.tb.hub
    assert w.story["S14"]["next"] in hub["turnaround"].blocked
    stored = " ".join(r["value"] for r in hub.store.q("SELECT value FROM feed_snapshots")).lower()
    assert "ignore all previous" not in stored and "without maintenance sign-off" not in stored
