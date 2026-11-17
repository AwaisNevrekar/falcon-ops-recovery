"""Phase 2: every guard rule gets a test that tries to break it."""
import pytest

from falcon import config as C
from falcon.feeds import FeedHub
from falcon.guards import GuardCtx, approval_role, run_guards, safe_action
from falcon.models import Action, WriteRejected
from falcon.replay import OpsDay
from falcon.solvers import stands as stand_solver


def world(at=None):
    w = OpsDay()
    if at:
        w.step(at)
    return w, GuardCtx(FeedHub(w))


def mk(type_, target, inputs=(), requires="", aid=None, **params):
    return Action(action_id=aid or f"t-{type_}-{target}", type=type_, target=target, params=params,
                  inputs=list(inputs), requires=requires)


def failed(actions, ctx, stage="plan"):
    return {r.rule for _, rs in run_guards(actions, ctx, stage)["removed"] for r in rs}


def approved_by(role, status="APPROVED"):
    return lambda action: {"status": status, "role": role, "decided_by": "test.controller"}


# ---------------------------------------------------------------- G0
def test_g0_unknown_action_types_cannot_exist():
    for t in ("RELEASE_AIRCRAFT", "CREATE_SIGNOFF", "OVERRIDE_GUARD", "delete_audit"):
        action, err = safe_action({"action_id": "x", "type": t, "target": "FG-W902"})
        assert action is None and err.startswith("G0")


def test_g0_category_cannot_be_relabelled_to_dodge_approval():
    a = Action(action_id="x", type="SWAP_CREW", target="FG346", category="GROUND")
    assert a.category == "CREW" and approval_role(a) == C.CREW_CTRL
    _, ctx = world()
    assert failed([mk("GROUND_DISPATCH", "FG532", measure="skip_safety_check")], ctx) == {"G0"}


# ---------------------------------------------------------------- G1 crew
def test_g1_blocks_standby_without_rest_and_allows_a_legal_one():
    w, ctx = world(891)
    cpt = w.story["S5"]["captain"]
    assert failed([mk("SWAP_CREW", "FG346", crew_out=cpt, crew_in="SBY-CPT-9", role="CPT")], ctx) == {"G1"}
    assert failed([mk("SWAP_CREW", "FG346", crew_out=cpt, crew_in="SBY-CPT-1", role="CPT")], ctx) == set()
    assert failed([mk("SWAP_CREW", "FG346", crew_out=cpt, crew_in="SBY-FO-1", role="CPT")], ctx) == {"G1"}   # wrong rank
    assert failed([mk("SWAP_CREW", "FG346", crew_out="CPT001", crew_in="SBY-CPT-1", role="CPT")], ctx) == {"G1"}


def test_g1_blocks_a_hold_that_pushes_crew_past_the_duty_limit():
    w, ctx = world(880)                       # captain of FG346 has exactly 40 min of margin
    assert failed([mk("HOLD_DEPARTURE", "FG346", minutes=20)], ctx) == set()
    w.legs["FG346"]["pdelay"] = 45            # the outstation turn absorbs at most 15 of these
    w.recompute()
    assert failed([mk("HOLD_DEPARTURE", "FG346", minutes=15)], ctx) == {"G1"}


def test_g1_fails_closed_when_crew_data_is_missing():
    w, ctx = world(880)
    w.add_fault("crew", "missing", until=w.now + 30, key="FG532")
    assert failed([mk("HOLD_DEPARTURE", "FG532", minutes=4)], ctx) == {"G1"}


# ---------------------------------------------------------------- G2 aircraft
def test_g2_blocks_an_aircraft_without_signoff_everywhere():
    w, ctx = world(906)                       # S6: spare FG-W902 has a tech fault and no sign-off
    swap = mk("SWAP_AIRCRAFT", "FG467", to_ac="FG-W902")
    assert failed([swap], ctx) == {"G2"}
    assert failed([swap], GuardCtx(ctx.hub, approved=approved_by(C.DUTY_OPS)), stage="execute") == {"G2"}   # not approvable
    with pytest.raises(WriteRejected):
        w.swap_aircraft("FG467", "FG-W902")   # the system of record refuses too
    assert failed([mk("SWAP_AIRCRAFT", "FG467", to_ac="FG-N903")], ctx) == {"G2"}       # wrong type
    assert failed([mk("SWAP_AIRCRAFT", "FG467", to_ac="FG-NOPE")], ctx) == {"G2"}       # unknown aircraft
    w.step(951)                               # Maintenance Control records the release
    assert failed([swap], ctx) == set()


def test_g2_expired_signoff_counts_as_no_signoff():
    w, ctx = world(851)
    w.aircraft["FG-N901"]["signoff"]["valid_until"] = w.now - 1
    assert failed([mk("SWAP_AIRCRAFT", w.story["S2"]["next"], to_ac="FG-N901")], ctx) == {"G2"}


# ---------------------------------------------------------------- G3 stands
def test_g3_blocks_wrong_stand_type_and_double_allocation():
    w, ctx = world(916)
    assert failed([mk("REASSIGN_STAND", "FG467", visit="FG467", to="A01")], ctx) == {"G3"}      # wide-body on a narrow stand
    c = next(c for c in w.stand_conflicts() if c["stand"] == w.story["S7"]["stand"])
    assert failed([mk("REASSIGN_STAND", c["b"], visit=c["b"], to=c["stand"])], ctx) == {"G3"}    # still occupied
    move = stand_solver.solve(w.visits(), w.stands, w.stand_conflicts())["moves"][0]
    good = mk("REASSIGN_STAND", move["visit"], visit=move["visit"], to=move["to"])
    assert failed([good], ctx) == set()
    clash = mk("REASSIGN_STAND", c["a"] if c["a"] != move["visit"] else c["b"], aid="other", to=move["to"],
               visit=c["a"] if c["a"] != move["visit"] else c["b"])
    assert "G3" in failed([good, clash], ctx)                      # two moves onto the same stand in one plan


# ---------------------------------------------------------------- G4 G5 G6 passengers
def test_g4_seat_capacity_is_checked_across_the_whole_plan():
    w, ctx = world(876)
    alt = w.story["S4"]["alts"][0]
    w.legs[alt]["free_y"] = 3
    pnrs = [g["pnr"] for g in w.read("pax", "out:FG718")[0] if g["size"] == 2 and g["cabin"] == "Y"][:2]
    plan = [mk("REBOOK_GROUP", p, pnr=p, size=2, cabin="Y", to=alt, aid=f"rb-{p}") for p in pnrs]
    assert failed(plan, ctx) == {"G4"}
    assert failed(plan[:1], ctx) == set()


def test_g5_no_rebooking_or_bag_transfer_below_minimum_connection_time():
    w, ctx = world(861)                       # S3: FG531 lands too late for FG532
    pnr = next(g["pnr"] for g in w.read("pax", "FG531")[0] if g["out"] == "FG532")
    assert "G5" in failed([mk("REBOOK_GROUP", pnr, pnr=pnr, size=2, cabin="Y", to="FG532", inb="FG531")], ctx)
    assert failed([mk("HOT_TRANSFER_BAGS", "FG531", inb="FG531", out="FG532")], ctx) == {"G5"}
    w.step(931)
    assert failed([mk("HOT_TRANSFER_BAGS", "FG853", inb="FG853", out=w.story["S8"]["outbounds"][0])], ctx) == set()


def test_g6_prm_and_unaccompanied_minor_rules():
    w, ctx = world(876)
    groups = w.read("pax", "out:FG718")[0]
    wchr = next(g["pnr"] for g in groups if "WCHR" in g["ssr"])
    umnr = next(g["pnr"] for g in groups if "UMNR" in g["ssr"])
    alt = w.story["S4"]["alts"][0]
    assert failed([mk("REBOOK_GROUP", wchr, pnr=wchr, size=1, cabin="Y", to=alt)], ctx) == set()
    w.legs[alt]["prm_assist"] = False
    assert failed([mk("REBOOK_GROUP", wchr, pnr=wchr, size=1, cabin="Y", to=alt)], ctx) == {"G6"}
    nd = "ND-" + w.legs["FG718"]["dest"]
    assert failed([mk("REBOOK_GROUP", umnr, pnr=umnr, size=1, cabin="Y", to=nd)], ctx) == {"G6"}   # never overnight
    w.legs[alt]["umnr_escort"] = False
    assert failed([mk("REBOOK_GROUP", umnr, pnr=umnr, size=1, cabin="Y", to=alt)], ctx) == {"G6"}


# ---------------------------------------------------------------- G7 money
def test_g7_voucher_and_hotel_caps():
    _, ctx = world()
    assert failed([mk("MEAL_VOUCHER", "FG718", amount_per_pax=C.VOUCHER_MAX_AED + 1, pax=10)], ctx) == {"G7"}
    assert failed([mk("MEAL_VOUCHER", "FG718", amount_per_pax=C.MEAL_VOUCHER_AED, pax=10)], ctx) == set()
    assert failed([mk("BOOK_HOTEL", "FG718", rate=C.HOTEL_MAX_RATE_AED + 300, rooms=5, pax=9)], ctx) == {"G7"}
    assert failed([mk("BOOK_HOTEL", "FG718", rate=C.HOTEL_RATE_AED, rooms=50, pax=9)], ctx) == {"G7"}  # more rooms than people


# ---------------------------------------------------------------- G8 data quality
def test_g8_holds_actions_built_on_stale_or_conflicting_data_but_lets_escalations_through():
    w, ctx = world(941)                       # S9: movements for FG644 are stale
    dep = w.story["S14"]["next"]
    assert failed([mk("HOLD_DEPARTURE", dep, minutes=4, inputs=["movements:FG644"])], ctx) == {"G8"}
    assert failed([mk("ESCALATE", "FG644", inputs=["movements:FG644"], to_role=C.DUTY_OPS)], ctx) == set()
    w.step(946)                               # S10: airline and ATC disagree about FG655
    assert failed([mk("HOLD_DEPARTURE", dep, minutes=4, inputs=["movements:FG655"])], ctx) == {"G8"}
    w.add_fault("inventory", "down", until=w.now + 9)
    assert failed([mk("HOLD_DEPARTURE", dep, minutes=4, inputs=["inventory:" + dep])], ctx) == {"G8"}
    w.step(958)                               # both feeds healthy again
    assert failed([mk("HOLD_DEPARTURE", dep, minutes=4, inputs=["movements:FG644", "movements:FG655"])], ctx) == set()


# ---------------------------------------------------------------- G9 approvals
def test_g9_schedule_aircraft_and_crew_changes_need_the_right_approval_record():
    w, ctx = world(891)
    hub = ctx.hub
    swap = mk("SWAP_AIRCRAFT", w.story["S2"]["next"], to_ac="FG-N901")
    assert failed([swap], ctx, "plan") == set()                                           # proposal only
    assert failed([swap], ctx, "execute") == {"G9"}                                       # no record -> never runs
    assert failed([swap], GuardCtx(hub, approved_by(C.CS_LEAD)), "execute") == {"G9"}      # wrong role
    assert failed([swap], GuardCtx(hub, approved_by(C.DUTY_OPS, "REJECTED")), "execute") == {"G9"}
    assert failed([swap], GuardCtx(hub, approved_by(C.DUTY_OPS)), "execute") == set()
    assert failed([swap], GuardCtx(hub, approved_by("OPS_MANAGER")), "execute") == set()   # escalation role
    crew = mk("SWAP_CREW", "FG346", crew_out=w.story["S5"]["captain"], crew_in="SBY-CPT-1", role="CPT")
    assert failed([crew], GuardCtx(hub, approved_by(C.DUTY_OPS)), "execute") == {"G9"}     # only the Crew Controller
    assert failed([crew], GuardCtx(hub, approved_by(C.CREW_CTRL)), "execute") == set()


def test_g9_hold_thresholds():
    _, ctx = world(861)
    assert failed([mk("HOLD_DEPARTURE", "FG532", minutes=C.HOLD_AUTO_MAX_MIN)], ctx, "execute") == set()   # autonomous
    assert failed([mk("HOLD_DEPARTURE", "FG532", minutes=12)], ctx, "execute") == {"G9"}
    assert failed([mk("HOLD_DEPARTURE", "FG532", minutes=12)], GuardCtx(ctx.hub, approved_by(C.DUTY_OPS)), "execute") == set()
    too_long = mk("HOLD_DEPARTURE", "FG532", minutes=C.HOLD_MAX_MIN + 25)
    assert failed([too_long], GuardCtx(ctx.hub, approved_by(C.DUTY_OPS)), "execute") == {"G9"}             # not approvable


def test_children_need_their_parent_and_are_removed_with_it():
    _, ctx = world()
    parent = mk("MEAL_VOUCHER", "FG718", amount_per_pax=9999, pax=3, aid="parent")
    child = mk("SEND_MESSAGE", "FG718", requires="parent", aid="child")
    out = run_guards([parent, child], ctx)
    assert not out["kept"] and {r.rule for _, rs in out["removed"] for r in rs} == {"G7", "DEP"}
    assert failed([child], ctx, "execute") == {"G9"}
    assert failed([child], GuardCtx(ctx.hub, executed=lambda aid: aid == "parent"), "execute") == set()


def test_mass_reaccommodation_and_hotel_budget_routing():
    assert approval_role(mk("MASS_REACCOMMODATION", "FG718", pax=310)) == C.CS_LEAD
    assert approval_role(mk("MASS_REACCOMMODATION", "FG718", pax=C.AUTO_REBOOK_MAX_PAX)) is None
    assert approval_role(mk("BOOK_HOTEL", "FG718", total_cost=C.HOTEL_AUTO_BUDGET_AED + 1)) == C.CS_LEAD
    assert approval_role(mk("GROUND_DISPATCH", "FG532", measure="priority_catering")) is None
    assert approval_role(mk("REASSIGN_STAND", "FG532", to="A01", boarding=True)) == C.DUTY_OPS
