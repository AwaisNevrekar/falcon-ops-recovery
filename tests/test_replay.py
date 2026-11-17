"""Phase 1: the generated day is deterministic and the replay world behaves."""
import json

import pytest

from falcon import config as C
from falcon.models import WriteRejected
from falcon.replay import OpsDay, generate
from falcon.solvers.propagation import compute, knock_on


def test_generation_is_deterministic():
    a = json.dumps(generate.__wrapped__(C.SEED), sort_keys=True, default=str)
    b = json.dumps(generate.__wrapped__(C.SEED), sort_keys=True, default=str)
    assert a == b
    assert a != json.dumps(generate.__wrapped__(C.SEED + 1), sort_keys=True, default=str)


def test_day_shape():
    d = generate(C.SEED)
    assert sum(1 for x in d["legs"].values() if x["kind"] == "DEP") == C.N_DEPARTURES
    assert sum(p["size"] for p in d["pnrs"].values()) > 55000
    assert {e["id"] for e in d["events"]} >= {f"S{i}" for i in range(1, 15)}
    s4 = [p for p in d["pnrs"].values() if p["out"] == "FG718"]
    assert sum(p["size"] for p in s4) == 310
    assert sum("WCHR" in p["ssr"] for p in s4) == 2 and sum("UMNR" in p["ssr"] for p in s4) == 1
    assert d["crew"]["SBY-CPT-9"]["rest_before"] < C.MIN_REST_MIN


def test_two_replays_are_identical():
    a, b = OpsDay(), OpsDay()
    for w in (a, b):
        for _ in range(60):
            w.step()
    assert a.metrics() == b.metrics()
    assert {f: (x["etd"], x["eta"]) for f, x in a.legs.items()} == {f: (x["etd"], x["eta"]) for f, x in b.legs.items()}


def test_late_inbound_cascades_down_the_rotation():
    w = OpsDay()
    w.step(851)
    assert w.legs["FG214"]["eta"] - w.legs["FG214"]["sta"] == 75
    nxt = w.legs[w.story["S2"]["next"]]
    assert nxt["etd"] - nxt["std"] >= 45              # 75 min late minus the planned slack
    assert w.legs[w._next(nxt["fid"])]["eta"] > w.legs[w._next(nxt["fid"])]["sta"]


def test_what_if_does_not_change_the_day():
    w = OpsDay()
    w.step(851)
    before = {f: (x["etd"], x["eta"], x["hold"]) for f, x in w.legs.items()}
    nxt = w.story["S2"]["next"]
    k = knock_on(w.legs, w.order, {nxt: {"turn_cut": 12}})
    assert k["delta_min"] < 0
    assert before == {f: (x["etd"], x["eta"], x["hold"]) for f, x in w.legs.items()}
    assert compute(w.legs, w.order)[nxt][0] == w.legs[nxt]["etd"]


def test_cancellation_strands_the_passengers():
    w = OpsDay()
    w.step(876)
    assert w.legs["FG718"]["cancelled"]
    assert sum(p["size"] for p in w.pnrs.values() if p["status"] == "CANCELLED") == 310


def test_the_day_itself_never_flies_an_illegal_crew_or_unsigned_aircraft():
    w = OpsDay()
    for m in range(w.now + 1, 1140):
        w.step(m)
    assert w.legs["FG346"]["atd"] is None and w.legs["FG346"]["blocked"] == "CREW_LIMIT"
    assert w.crew_violations() == 0
    w.aircraft[w.legs[w.story["S14"]["next"]]["ac"]]["signoff"] = None
    assert w.blocked(w.story["S14"]["next"]) == "NO_SIGNOFF"


def test_signoff_can_only_come_from_a_human_in_maintenance():
    w = OpsDay()
    for who in ("agent-crew", "system", "auto", "LLM", ""):
        with pytest.raises(WriteRejected):
            w.record_signoff("FG-W902", who)
    w.record_signoff("FG-W902", "MX-CTRL-07")
    assert w.aircraft["FG-W902"]["signoff"]["by"] == "MX-CTRL-07"


def test_pax_feed_never_exposes_booking_refs():
    w = OpsDay()
    refs = {p["ref"] for p in w.pnrs.values() if p["out"] == "FG718"}
    text = json.dumps(w.read("pax", "out:FG718")[0])
    assert "ref" not in json.loads(text)[0]
    assert not any(r in text for r in refs)


def test_seat_reservation_respects_capacity():
    w = OpsDay()
    f = w.story["S13"]["alt"]
    w.legs[f]["free_y"] = 1
    w.reserve(f, "Y")
    with pytest.raises(WriteRejected):
        w.reserve(f, "Y")
