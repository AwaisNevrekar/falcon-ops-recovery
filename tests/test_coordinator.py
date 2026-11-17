"""Phase 3: coordinator plan check (graphlib) — reject, replan once, then rule plan."""
import pytest

from falcon.coordinator import AGENTS, RULE_PLANS, check_plan, plan_case, rule_plan
from falcon.models import PlanStep


def case(type_="LATE_INBOUND", phase="A"):
    return {"case_id": "C-T", "type": type_, "flight": "FG214", "inbound": "FG214", "departure": "FG1049", "phase": phase}


def steps(d):
    return [PlanStep(agent=a, after=list(deps)) for a, deps in d.items()]


class FakeLLM:
    enabled = True

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def chat_json(self, system, user, tag=""):
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else None


def as_reply(d):
    return {"steps": [{"agent": a, "after": deps} for a, deps in d.items()], "reason": "test"}


@pytest.mark.parametrize("key", list(RULE_PLANS))
def test_every_rule_plan_passes_the_check(key):
    c = case(*key)
    ok, err, waves = check_plan(rule_plan(c), c)
    assert ok, err
    flat = [a for w in waves for a in w]
    assert set(flat) <= set(AGENTS)
    if "crew_aircraft" in flat:                       # crew check and maintenance read come first
        assert flat.index("crew_legality") < flat.index("crew_aircraft")
        assert flat.index("maintenance_check") < flat.index("crew_aircraft")
    if "reaccommodation" in flat:
        assert flat.index("reaccommodation") < flat.index("passenger")


def test_parallel_waves():
    c = case()
    _, _, waves = check_plan(rule_plan(c), c)
    assert waves[0] == ["maintenance_check", "propagation"]
    assert {"turnaround", "passenger", "baggage", "crew_legality"} <= set(waves[1])       # run in parallel
    assert waves[-1] == ["crew_aircraft"]


GOOD = RULE_PLANS[("LATE_INBOUND", "A")]


@pytest.mark.parametrize("plan,phase,expect", [
    ({**GOOD, "propagation": ["crew_aircraft"]}, "A", "cycle"),
    ({**GOOD, "release_aircraft": []}, "A", "unknown agent"),
    ({k: v for k, v in GOOD.items() if k != "turnaround"}, "A", "required agent 'turnaround'"),
    ({**GOOD, "crew_aircraft": ["maintenance_check", "propagation"], "crew_legality": ["crew_aircraft"]}, "A", "crew_legality must finish before crew_aircraft"),
    ({**GOOD, "crew_aircraft": ["crew_legality", "propagation"], "maintenance_check": ["crew_aircraft"]}, "A", "maintenance_check must finish before crew_aircraft"),
    ({**GOOD, "reaccommodation": ["propagation"]}, "A", "only run after the schedule option is fixed"),
    ({"propagation": [], "reaccommodation": ["propagation"], "passenger": ["propagation"]}, "B", "reaccommodation must finish before passenger"),
    ({**GOOD, "turnaround": ["ghost"]}, "A", "not in the plan"),
    ({}, "A", "empty"),
])
def test_bad_plans_are_rejected(plan, phase, expect):
    ok, err, _ = check_plan(steps(plan), case(phase=phase))
    assert not ok and expect in err


def test_crew_aircraft_without_its_checks_is_rejected():
    plan = {k: v for k, v in GOOD.items() if k != "crew_legality"}
    plan["crew_aircraft"] = ["maintenance_check", "propagation"]
    ok, err, _ = check_plan(steps(plan), case())
    assert not ok and "crew_legality" in err


def test_duplicate_agent_rejected():
    ok, err, _ = check_plan(steps(GOOD) + [PlanStep(agent="turnaround")], case())
    assert not ok and "twice" in err


def test_valid_llm_plan_is_used():
    out = plan_case(case(), FakeLLM([as_reply(GOOD)]))
    assert out["source"] == "llm" and [a["ok"] for a in out["attempts"]] == [True]


def test_rejected_plan_is_sent_back_once_with_the_error():
    bad = {k: v for k, v in GOOD.items() if k != "maintenance_check"}
    bad["crew_aircraft"] = ["crew_legality", "propagation"]
    llm = FakeLLM([as_reply(bad), as_reply(GOOD)])
    out = plan_case(case(), llm)
    assert out["source"] == "llm" and [a["ok"] for a in out["attempts"]] == [False, True]
    assert "previous_plan_rejected_because" in llm.prompts[1] and "maintenance_check" in llm.prompts[1]


def test_two_bad_plans_fall_back_to_the_rule_plan_and_both_are_logged():
    cyc = as_reply({**GOOD, "propagation": ["crew_aircraft"]})
    out = plan_case(case(), FakeLLM([cyc, {"steps": "nonsense"}]))
    assert out["source"] == "rule"
    assert [(a["source"], a["ok"]) for a in out["attempts"]] == [("llm", False), ("llm", False), ("rule", True)]
    assert check_plan([PlanStep(**s) for s in out["steps"]], case())[0]


def test_no_llm_means_rule_plan():
    out = plan_case(case("CANCELLATION", "B"), None)
    assert out["source"] == "rule" and out["waves"][0] == ["reaccommodation"]
    out = plan_case(case(), FakeLLM([None]))
    assert out["source"] == "rule" and out["attempts"][0]["error"] == "LLM unavailable"
