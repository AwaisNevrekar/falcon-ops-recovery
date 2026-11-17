"""Phase 2: deterministic solvers — fair re-accommodation, stands, crew legality."""
import random

import pytest

from falcon import config as C
from falcon.replay import OpsDay
from falcon.solvers import crew_legality
from falcon.solvers import reaccommodation as R
from falcon.solvers import stands as S


def grp(pnr, size=1, cabin="Y", ssr=(), family=False, onward=None, ready=600, orig_std=500, booked=0, loyalty=0):
    return {"pnr": pnr, "size": size, "cabin": cabin, "ssr": list(ssr), "family": family, "onward": onward,
            "ready": ready, "orig_std": orig_std, "orig_sta": 700, "booked": booked, "loyalty": loyalty}


def flt(fid, etd, free_y=0, free_j=0, prm=True, escort=True, nd=False):
    return {"fid": fid, "etd": etd, "eta": etd + 100, "free_y": free_y, "free_j": free_j, "prm_assist": prm,
            "umnr_escort": escort, "next_day": nd}


def placed(result):
    return {a["pnr"]: a["to"] for a in result["assignments"]}


MIX = [grp("e-regular", loyalty=3), grp("d-tight", onward="tight"), grp("c-family", family=True),
       grp("b-last", onward="last"), grp("a-wchr", ssr=["WCHR"])]


@pytest.mark.parametrize("seats", [1, 2, 3, 4])
def test_scarce_early_seats_go_in_the_published_priority_order(seats):
    res = R.solve(MIX, [flt("EARLY", 700, free_y=seats), flt("LATE", 1100, free_y=9)])
    on_early = sorted(p for p, f in placed(res).items() if f == "EARLY")
    assert on_early == sorted(["a-wchr", "b-last", "c-family", "d-tight"][:seats])   # loyalty never jumps the queue


def test_inside_class_e_earliest_original_departure_then_booking_time_wins():
    gs = [grp("late-dep", orig_std=560), grp("early-dep", orig_std=500, booked=5), grp("early-dep-booked-first", orig_std=500, booked=1)]
    res = R.solve(gs, [flt("EARLY", 700, free_y=1), flt("MID", 800, free_y=1), flt("LATE", 1100, free_y=1)])
    assert placed(res) == {"early-dep-booked-first": "EARLY", "early-dep": "MID", "late-dep": "LATE"}


def test_loyalty_only_breaks_ties():
    gs = [grp("gold", loyalty=3), grp("basic", loyalty=0)]
    assert placed(R.solve(gs, [flt("EARLY", 700, free_y=1), flt("LATE", 1100, free_y=1)]))["gold"] == "EARLY"


def test_a_group_is_never_split():
    fam = [grp("family4", size=4, family=True)]
    res = R.solve(fam, [flt("F1", 700, free_y=3), flt("F2", 800, free_y=3), flt("ND", 1900, free_y=400, nd=True)])
    assert placed(res) == {"family4": "ND"} and res["assignments"][0]["overnight"]
    res = R.solve(fam, [flt("F1", 700, free_y=3), flt("F2", 800, free_y=3)])
    assert not res["assignments"] and res["unassigned"][0]["reasons"] == ["NO_SEATS_FOR_WHOLE_GROUP"]


def test_prm_needs_assistance_and_umnr_needs_same_day_escort():
    gs = [grp("wchr", ssr=["WCHR"]), grp("umnr", ssr=["UMNR"])]
    res = R.solve(gs, [flt("NOASSIST", 700, free_y=9, prm=False, escort=False), flt("OK", 900, free_y=9),
                       flt("ND", 1900, free_y=400, nd=True)])
    assert placed(res) == {"wchr": "OK", "umnr": "OK"}
    res = R.solve(gs, [flt("NOESCORT", 700, free_y=9, escort=False), flt("ND", 1900, free_y=400, nd=True)])
    assert placed(res) == {"wchr": "NOESCORT"}                      # the minor is never sent overnight or unescorted
    assert res["unassigned"][0]["pnr"] == "umnr" and "UMNR_NEEDS_SAME_DAY_ESCORT" in res["unassigned"][0]["reasons"]


def test_flights_leaving_before_the_passenger_is_ready_are_not_used():
    res = R.solve([grp("x", ready=750)], [flt("TOO-EARLY", 700, free_y=9), flt("OK", 800, free_y=9)])
    assert placed(res) == {"x": "OK"}


def test_business_keeps_its_cabin_when_possible_and_downgrades_are_flagged():
    res = R.solve([grp("j", cabin="J")], [flt("F", 700, free_y=9, free_j=1)])
    assert res["assignments"][0]["to_cabin"] == "J"
    res = R.solve([grp("j", cabin="J")], [flt("F", 700, free_y=9, free_j=0)])
    assert res["assignments"][0]["to_cabin"] == "Y" and "R3-downgrade" in res["assignments"][0]["rules"]
    assert not R.solve([grp("y")], [flt("F", 700, free_y=0, free_j=5)])["assignments"]      # no free upgrades


def _random_case(seed=7):
    rng = random.Random(seed)
    gs = [grp(f"p{i:03d}", size=rng.choice((1, 1, 2, 3, 5)), ssr=["WCHR"] if i % 23 == 0 else [],
              family=i % 7 == 0, onward=rng.choice((None, None, "tight", "last")), orig_std=500 + i % 5,
              booked=-rng.randrange(9999)) for i in range(120)]
    fs = [flt(f"F{i}", 700 + 60 * i, free_y=rng.randrange(10, 40), prm=i != 0) for i in range(5)]
    return gs, fs + [flt("ND", 1900, free_y=400, nd=True)]


def test_capacity_is_never_exceeded_and_result_is_reproducible():
    gs, fs = _random_case()
    a, b = R.solve(gs, fs), R.solve(gs, fs)
    assert a == b
    used = {}
    for x in a["assignments"]:
        used[x["to"]] = used.get(x["to"], 0) + x["size"]
        assert all(r.startswith("R") for r in x["rules"]) and x["why"]
    assert all(used.get(f["fid"], 0) <= f["free_y"] for f in fs)
    assert a["stats"]["assigned_pax"] == sum(g["size"] for g in gs)
    assert all(x["to"] != "F0" for x in a["assignments"] if x["pnr"] in {g["pnr"] for g in gs if g["ssr"]})


def test_pulp_fallback_gives_the_same_answer():
    fs = [flt("EARLY", 700, free_y=2), flt("LATE", 1100, free_y=9)]
    assert placed(R.solve(MIX, fs, backend="pulp")) == placed(R.solve(MIX, fs, backend="cpsat"))


def test_stand_solver_clears_the_scripted_double_allocation():
    w = OpsDay()
    w.step(916)                                                    # S7: FG611 planned onto an occupied stand
    before = w.stand_conflicts()
    s7 = w.story["S7"]
    assert any(c["stand"] == s7["stand"] and s7["next"] in (c["a"], c["b"]) for c in before)
    res = S.solve(w.visits(), w.stands, before)
    assert res["moves"] and not res["unresolved"]
    parked = {v["id"] for v in w.visits() if v["on_stand"]}
    for mv in res["moves"]:
        assert mv["visit"] not in parked                           # parked aircraft are never moved
        assert w.legs[mv["visit"]]["type"] in w.stands[mv["to"]]["types"]
        w.set_stand(mv["visit"], mv["to"])
    assert not w.stand_conflicts()
    assert S.solve(w.visits(), w.stands, before) == S.solve(w.visits(), w.stands, before)


def test_crew_legality_margin_and_legal_options_only():
    w = OpsDay()
    w.step(880)
    cpt = w.story["S5"]["captain"]
    ok = crew_legality.check(w.crew[cpt], w.crew_times(cpt, "FG346"), w.now)
    assert ok["legal"] and ok["margin_min"] == 40
    w.step(891)                                                    # S5: FG346 will now leave 60 min late
    bad = crew_legality.check(w.crew[cpt], w.crew_times(cpt, "FG346"), w.now)
    assert not bad["legal"] and bad["margin_min"] < 0 and bad["reasons"][0].startswith("DUTY")
    standby = w.read("crew", "standby")[0]
    opts = crew_legality.legal_options("CPT", w.crew_times(cpt, "FG346"), standby, w.now)
    offered = [o["crew"] for o in opts["options"]]
    assert offered and "SBY-CPT-9" not in offered                  # not enough rest: never offered
    assert all(o["margin_min"] >= 0 for o in opts["options"])
    assert any(r["crew"] == "SBY-CPT-9" and r["reasons"][0].startswith("REST") for r in opts["rejected"])
    assert all(o["crew"].startswith("SBY-CPT") for o in opts["options"])


def test_sector_limit():
    m = {"id": "X", "role": "CPT", "duty_start": 0, "rest_before": 900, "legs": ["a"] * (C.MAX_SECTORS + 1)}
    assert not crew_legality.check(m, [("a", 10, 60)], 5)["legal"]
