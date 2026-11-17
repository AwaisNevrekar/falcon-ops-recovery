"""Phase 4: the write gateway and the saga — every write re-checked, every step undoable."""
import copy

import pytest

from falcon import config as C
from falcon.approvals import Approvals
from falcon.audit import Audit
from falcon.feeds import FeedHub
from falcon.models import Action
from falcon.replay import OpsDay
from falcon.saga import WriteGateway
from falcon.store import Store


def setup(at=None):
    w = OpsDay()
    if at:
        w.step(at)
    store = Store()
    hub = FeedHub(w, store)
    appr = Approvals(store, w)
    return w, WriteGateway(w, hub, store, Audit(store, w), appr), appr, store


def mk(type_, target, bundle="", requires="", aid=None, inputs=(), **params):
    return Action(action_id=aid or f"s-{type_}-{target}", type=type_, target=target, params=params, bundle=bundle,
                  requires=requires, inputs=list(inputs))


def steps(store):
    return [(r["step"], r["status"]) for r in store.q("SELECT step, status FROM saga_steps ORDER BY id")]


def test_failed_step_three_undoes_steps_two_and_one_in_reverse_order():
    w, gw, _, store = setup(1000)                      # S13: 3 seats left, one is sold while we book
    s = w.story["S13"]
    fam, alt = w.pnrs[s["family"]], s["alt"]
    assert w.legs[alt]["free_y"] == 3 and fam["size"] == 3
    res = gw.run_bundle("C-T", [mk("REBOOK_GROUP", fam["id"], pnr=fam["id"], size=3, cabin="Y", to=alt, inb=fam["inb"])])
    assert not res["ok"] and res["kind"] == "write" and "inventory changed" in res["error"]
    assert steps(store) == [
        (f"reserve seat 1/3 on {alt} for {fam['id']}", "DONE"), (f"reserve seat 2/3 on {alt} for {fam['id']}", "DONE"),
        (f"reserve seat 3/3 on {alt} for {fam['id']}", "FAILED"),
        (f"UNDO reserve seat 2/3 on {alt} for {fam['id']}", "UNDONE"),
        (f"UNDO reserve seat 1/3 on {alt} for {fam['id']}", "UNDONE")]
    assert w.legs[alt]["free_y"] == 2                  # our two seats were given back; the outside sale stands
    assert fam["cur"] == s["outbound"] and fam["status"] == "OK"     # the family is still together on its booking


def test_a_bundle_is_all_or_nothing():
    w, gw, _, store = setup(876)
    g = next(p for p in w.read("pax", "out:FG718")[0] if p["size"] == 2 and p["cabin"] == "Y")
    alt = w.story["S4"]["alts"][0]
    free = w.legs[alt]["free_y"]
    ok = [mk("REBOOK_GROUP", g["pnr"], bundle="b1", aid="rb", pnr=g["pnr"], size=2, cabin="Y", to=alt),
          mk("REROUTE_BAGS", g["pnr"], bundle="b1", requires="rb", aid="bags", pnr=g["pnr"], to=alt)]
    bad = ok + [mk("MEAL_VOUCHER", "FG718", bundle="b1", aid="v", pnrs={g["pnr"]: 2}, amount_per_pax=9999)]
    res = gw.run_bundle("C-T", bad)                    # the voucher breaks rule G7 -> everything before it is undone
    assert not res["ok"] and res["kind"] == "guard" and res["rule"] == "G7" and res["undone"] == ["bags", "rb"]
    assert w.legs[alt]["free_y"] == free and w.pnrs[g["pnr"]]["cur"] == "FG718" and w.pnrs[g["pnr"]]["bags_to"] == "FG718"
    res = gw.run_bundle("C-T", ok)
    assert res["ok"] and w.pnrs[g["pnr"]]["cur"] == alt and w.pnrs[g["pnr"]]["bags_to"] == alt
    assert w.legs[alt]["free_y"] == free - 2


def test_gateway_requires_the_approval_record_at_write_time():
    w, gw, appr, store = setup(851)
    n = w.story["S2"]["next"]
    swap = mk("SWAP_AIRCRAFT", n, to_ac="FG-N901")
    res = gw.run_bundle("C-FG214", [swap])
    assert not res["ok"] and res["rule"] == "G9" and w.legs[n]["ac"] != "FG-N901" and gw.writes == 0
    aid = appr.request("C-FG214", swap)
    appr.decide(aid, "REJECTED", "duty.controller")
    assert not gw.run_bundle("C-FG214", [swap])["ok"]                      # a rejection is not an approval
    store.set("approvals", "approval_id", aid, status="PENDING")
    appr.decide(aid, "APPROVED", "duty.controller")
    old = w.legs[n]["ac"]
    assert gw.run_bundle("C-FG214", [swap])["ok"] and w.legs[n]["ac"] == "FG-N901"
    assert w.rot["FG-N901"][0] == n and n not in w.rot[old]


def test_approved_swap_onto_an_unsigned_aircraft_is_still_refused():
    w, gw, appr, _ = setup(906)                        # S6: FG-W902 has no sign-off
    swap = mk("SWAP_AIRCRAFT", "FG467", to_ac="FG-W902")
    appr.decide(appr.request("C-FG466", swap), "APPROVED", "duty.controller")
    res = gw.run_bundle("C-FG466", [swap])
    assert not res["ok"] and res["rule"] == "G2" and w.legs["FG467"]["ac"] != "FG-W902"


def test_nothing_in_the_gateway_can_release_an_aircraft():
    w, gw, _, _ = setup(906)
    forged = Action.model_construct(action_id="x", type="RELEASE_AIRCRAFT", target="FG-W902", params={}, category="GROUND",
                                    inputs=[], requires="", bundle="", undo=None, why="", agent="")
    res = gw.run_bundle("C-T", [forged])
    assert not res["ok"] and res["rule"] == "G0" and "unknown action type" in res["error"]
    with pytest.raises(Exception, match="no write handler"):
        gw.steps("C-T", forged)                        # and even past the guard there is simply no handler for it
    assert w.aircraft["FG-W902"]["signoff"] is None and gw.writes == 0


def test_partner_outage_fails_the_saga_cleanly_and_other_writes_still_work():
    w, gw, _, _ = setup(876)
    for f in w.story["S4"]["alts"]:
        w.legs[f]["free_y"] = 0
    nd = "ND-" + w.legs["FG718"]["dest"]
    gs = [p for p in w.read("pax", "out:FG718")[0] if p["cabin"] == "Y" and not p["ssr"]][:2]
    for g in gs:
        assert gw.run_bundle("C-T", [mk("REBOOK_GROUP", g["pnr"], aid="rb" + g["pnr"], pnr=g["pnr"], size=g["size"], cabin="Y", to=nd)])["ok"]
    rooms = {g["pnr"]: 1 for g in gs}
    hotel = mk("BOOK_HOTEL", "FG718", rooms_by_pnr=rooms, rooms=2, pax=3, rate=C.HOTEL_RATE_AED, total_cost=840)
    w.add_fault("hotel", "down", until=w.now + 40)
    res = gw.run_bundle("C-T", [hotel])
    assert not res["ok"] and res["kind"] == "partner" and not w.hotel["bookings"]
    assert gw.run_bundle("C-T", [mk("GROUND_DISPATCH", w.story["S2"]["next"], measure="priority_catering")])["ok"]
    for _ in range(45):
        w.step()
    assert gw.run_bundle("C-T", [hotel])["ok"] and set(w.hotel["bookings"]) == set(rooms)


def test_every_action_type_can_be_undone():
    w, gw, appr, _ = setup(931)
    s = w.story
    n2, out8 = s["S2"]["next"], s["S8"]["outbounds"][1]
    visit = next(v for v in w.visits() if not v["on_stand"] and v["type"] == "NB")
    free = next(sid for sid, st in w.stands.items() if not st["contact"] and "NB" in st["types"]
                and all(v["stand"] != sid for v in w.visits()))
    cpt = w.legs["FG346"]["crew"][0]
    acts = [mk("GROUND_DISPATCH", n2, bundle="b", aid="a1", measure="extra_cleaning_team"),
            mk("HOLD_DEPARTURE", n2, bundle="b", aid="a2", minutes=4),
            mk("HOT_TRANSFER_BAGS", "FG853", bundle="b", aid="a3", inb="FG853", out=out8, runs=1),
            mk("PRIORITY_TAG_BAGS", "FG853", bundle="b", aid="a4", inb="FG853", out=out8),
            mk("REASSIGN_STAND", visit["id"], bundle="b", aid="a5", visit=visit["id"], to=free),
            mk("SWAP_AIRCRAFT", n2, bundle="b", aid="a6", to_ac="FG-N901"),
            mk("SWAP_CREW", "FG346", bundle="b", aid="a7", crew_out=cpt, crew_in="SBY-CPT-2", role="CPT"),
            mk("ESCALATE", n2, bundle="b", aid="a8", to_role=C.DUTY_OPS, reason="test"),
            mk("MEAL_VOUCHER", n2, bundle="b", aid="a9", pnrs={}, amount_per_pax=C.VOUCHER_MAX_AED + 1)]   # fails -> undo all
    for a in acts[5:7]:
        appr.decide(appr.request("C-T", a), "APPROVED", "controller")
    w.recompute()
    before = (copy.deepcopy(w.legs), copy.deepcopy(w.rot), copy.deepcopy(w.crew), dict(w.resources),
              {k: (p["hot"], p["rush"]) for k, p in w.pnrs.items()})
    res = gw.run_bundle("C-T", acts)
    assert not res["ok"] and res["failed_action"] == "a9" and res["steps_done"] == 8
    assert res["undone"] == ["a8", "a7", "a6", "a5", "a4", "a3", "a2", "a1"]
    after = (w.legs, w.rot, w.crew, {k: v for k, v in w.resources.items()}, {k: (p["hot"], p["rush"]) for k, p in w.pnrs.items()})
    for name in ("measure_win",):
        for leg in list(before[0].values()) + list(w.legs.values()):
            leg.pop(name, None)
    assert before[0] == after[0] and before[1] == after[1] and before[2] == after[2] and before[4] == after[4]
    assert {k: v for k, v in before[3].items() if v} == {k: v for k, v in after[3].items() if v}


def test_stale_input_at_write_time_holds_the_action():
    w, gw, _, _ = setup(941)                           # S9: movements for FG644 are stale right now
    n = w._next("FG644")
    res = gw.run_bundle("C-T", [mk("GROUND_DISPATCH", n, measure="priority_catering", inputs=["movements:FG644"])])
    assert not res["ok"] and res["kind"] == "data" and res["rule"] == "G8" and not w.legs[n]["measures"]
