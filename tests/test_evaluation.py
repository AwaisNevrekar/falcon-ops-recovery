"""Phase 6: baseline policy, evaluation report and the business-case model."""
import json
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from falcon import business_case, evaluation  # noqa: E402
from falcon import config as C  # noqa: E402
from falcon.baseline import Baseline  # noqa: E402


@pytest.fixture(scope="module")
def res():
    return evaluation.evaluate()


def test_baseline_is_manual_but_never_unsafe():
    b = Baseline()
    b.run_until(1140)
    w = b.world
    assert all(x["hold"] == 0 and x["turn_cut"] == 0 for x in w.legs.values())          # no holds, no compression
    assert not any(p["hot"] for p in w.pnrs.values())                                   # no hot transfers
    assert w.crew_violations() == 0                                                     # the day never breaks a limit
    assert b.counts["swaps"] >= 3 and all(t >= 850 + C.MANUAL_DECISION_MIN for t, kind, *_ in b.log if kind == "SWAP_AIRCRAFT")
    cancelled = [p for p in w.pnrs.values() if p["out"] == "FG718" and p["rebooked_at"] is not None]
    assert cancelled and min(p["rebooked_at"] for p in cancelled) >= 875 + C.MANUAL_DECISION_MIN
    rate = C.MANUAL_AGENTS * C.MANUAL_REBOOK_PAX_PER_AGENT_HOUR / 60                     # desk capacity is respected
    first, last = min(p["rebooked_at"] for p in cancelled), max(p["rebooked_at"] for p in cancelled)
    assert sum(p["size"] for p in cancelled) <= rate * (last - first + 1) + 10


def test_same_day_same_seed_for_both_runs(res):
    assert res["meta"]["seed"] == C.SEED
    b, s = res["baseline"], res["system"]
    assert b["cancelled_flights"] == s["cancelled_flights"] == 1
    assert evaluation.evaluate()["baseline"] == b                                        # reproducible


def test_system_beats_the_manual_baseline_where_it_should(res):
    b, s = res["baseline"], res["system"]
    assert s["reactionary_delay_min"] < 0.7 * b["reactionary_delay_min"]
    assert s["departure_delay_min"] < b["departure_delay_min"]
    assert s["misconnected_bags"] < 0.7 * b["misconnected_bags"]
    assert s["rebook_time_avg_min"] < 0.25 * b["rebook_time_avg_min"]
    assert s["mass_rebook_complete_min"] < 0.25 * b["mass_rebook_complete_min"]
    assert s["missed_connections_pax"] <= b["missed_connections_pax"]
    assert s["prm_umnr_protected"] >= b["prm_umnr_protected"] and s["prm_umnr_protected"] == s["prm_umnr_disrupted"]
    assert s["not_reaccommodated_pax"] == 0
    assert s["crew_legality_violations"] == 0 and b["crew_legality_violations"] == 0
    st = s["stats"]
    assert st["actions_auto"] > 0 and st["actions_approved"] > 0 and st["approvals_timed_out"] == 1
    assert st["llm_calls"] == 0 and "rule-based" in st["llm_mode"]


def test_reports_are_written(res, tmp_path):
    path = evaluation.write(res, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "Manual baseline" in text and "Crew legality violations" in text and "does not show" in text
    assert json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))["system"]["stats"]["cases"] > 50
    assert (tmp_path / "evaluation.png").stat().st_size > 10000
    bc = (tmp_path / "BUSINESS_CASE.md").read_text(encoding="utf-8")
    assert "Ground-handling partner" in bc and "Assumptions (all editable)" in bc and "Payback" in bc
    assert evaluation.load(tmp_path)["baseline"] == res["baseline"]


def test_business_case_is_quantity_times_assumption(res):
    a = business_case.defaults()
    bc = business_case.compute(a, res)
    b, s = res["baseline"], res["system"]
    assert bc["airline"]["Delay cost avoided"] == (b["departure_delay_min"] - s["departure_delay_min"]) * a["delay_cost_per_min"]
    assert bc["per_day"]["total"] == pytest.approx(sum(bc["airline"].values()) + sum(bc["handler"].values()))
    assert bc["per_day"]["airline"] > 0 and bc["per_day"]["handler"] > 0 and bc["staff_hours_per_day"]["saved"] > 20
    assert bc["handler"]["Extra ground teams dispatched (cost)"] < 0                    # the handler's extra cost is shown
    assert 0 < bc["payback_months"] < 60
    double = business_case.compute({**a, "delay_cost_per_min": 2 * a["delay_cost_per_min"]}, res)
    assert double["airline"]["Delay cost avoided"] == 2 * bc["airline"]["Delay cost avoided"]
    zero = business_case.compute({k: 0.0 for k in a}, res)
    assert zero["per_day"]["total"] == 0 and zero["payback_months"] == float("inf")
    assert set(business_case.explain(a, bc)) == set(bc["airline"]) | set(bc["handler"])
