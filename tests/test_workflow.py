"""Phase 4: the LangGraph workflow end to end — scenarios, approvals, wait/resume, audit, isolation."""
import os
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

import scenarios  # noqa: E402
from falcon import config as C  # noqa: E402
from falcon import workflow  # noqa: E402
from falcon.store import loads  # noqa: E402
from falcon.workflow import Engine  # noqa: E402


@pytest.fixture(scope="module")
def day():
    return scenarios.run()


@pytest.fixture(scope="module")
def results(day):
    return {r["id"]: r for r in scenarios.check(day)}


@pytest.mark.parametrize("sid", [s for s, _ in scenarios.CHECKS] + ["SAFETY"])
def test_scenario(results, sid):
    assert results[sid]["result"] == "PASS", results[sid]["evidence"]


def hold_request(e):
    return next(r for r in e.approvals.inbox(C.DUTY_OPS) if r["signature"].startswith("HOLD_DEPARTURE|FG532"))


def test_a_pending_approval_pauses_only_its_own_case():
    e = Engine()                                        # manual approvers: nobody answers
    e.run_until(858)
    # C-FG214 has been waiting for the Duty Ops Controller since 14:10 ...
    assert e.cases["C-FG214"]["status"] == "WAITING_APPROVAL" and e.cases["C-FG214"]["opened_at"] == 850
    assert e.graph.get_state({"configurable": {"thread_id": "C-FG214"}}).next == ("approval",)
    assert e.world.legs[e.world.story["S2"]["next"]]["ac"] != "FG-N901"          # the swap has NOT happened
    assert e.store.q("SELECT 1 FROM actions WHERE case_id='C-FG214' AND status='EXECUTED'")   # its automatic part ran
    # ... and an unanswered request does not stop the next case from running every stage.
    e.run_until(861)
    assert e.cases["C-FG531"]["opened_at"] == 860 and e.cases["C-FG531"]["status"] == "WAITING_APPROVAL"
    ran = e.store.q("SELECT COUNT(*) AS n FROM audit_log WHERE case_id='C-FG531' AND kind='PROPOSAL' AND t>=860")[0]["n"]
    assert ran >= 6 and e.world.legs["FG532"]["hold"] == 0
    e.run_until(871)                                    # nobody answered within 10 minutes
    rows = {r["case_id"]: r["status"] for r in e.store.q("SELECT case_id, status FROM approvals WHERE role=? AND "
                                                         "status!='NOTICE' AND case_id IN ('C-FG214','C-FG531')", C.DUTY_OPS)}
    assert rows == {"C-FG214": "TIMEOUT", "C-FG531": "TIMEOUT"}
    assert e.world.legs["FG532"]["hold"] == 0 and e.cases["C-FG531"]["status"] == "WAITING"   # safe default, escalated


def test_approve_executes_and_reject_replans():
    e = Engine()
    e.run_until(861)
    row = hold_request(e)
    with pytest.raises(ValueError):
        e.decide(row["approval_id"], "APPROVED", "")    # an approval needs a named approver
    e.decide(row["approval_id"], "APPROVED", "irfan.dutyops", "protect the connection")
    assert e.world.legs["FG532"]["hold"] == 12
    with pytest.raises(ValueError):
        e.decide(row["approval_id"], "REJECTED", "someone.else")       # already decided
    act = e.store.one("SELECT * FROM actions WHERE action_id=?", row["action_id"])
    assert act["status"] == "EXECUTED" and act["decided_by"].startswith("irfan.dutyops")

    e2 = Engine()
    e2.run_until(861)
    e2.decide(hold_request(e2)["approval_id"], "REJECTED", "irfan.dutyops", "slot too tight")
    assert e2.world.legs["FG532"]["hold"] == 0
    stranded = [p for p in e2.world.pnrs.values() if p["inb"] == "FG531" and p["out"] == "FG532"]
    assert all(p["status"] == "REBOOKED" and p["cur"] != "FG532" for p in stranded)     # re-planned: rebooked instead
    assert e2.store.one("SELECT status FROM actions WHERE action_id=?", hold_request_id(e2))["status"] == "REJECTED"


def hold_request_id(e):
    return e.store.one("SELECT action_id FROM approvals WHERE signature LIKE 'HOLD_DEPARTURE|FG532%'")["action_id"]


def test_case_sleeps_in_the_sqlite_checkpointer_and_resumes_where_it_stopped(tmp_path):
    db = str(tmp_path / "falcon_state.db")
    e = Engine(db_path=db)
    e.run_until(861)
    assert os.path.exists(db + ".ckpt.db")
    before = e.store.q("SELECT COUNT(*) AS n FROM proposals WHERE case_id='C-FG531' AND iteration=1")[0]["n"]
    e.decide(hold_request(e)["approval_id"], "APPROVED", "irfan.dutyops")
    after = e.store.q("SELECT COUNT(*) AS n FROM proposals WHERE case_id='C-FG531' AND iteration=1")[0]["n"]
    assert before == after and e.world.legs["FG532"]["hold"] == 12     # agents were not re-run for the approved plan
    assert e.store.q("SELECT COUNT(*) AS n FROM proposals WHERE case_id='C-FG531' AND iteration=2")[0]["n"] > 0   # phase B ran


def test_audit_trace_links_action_to_data_rules_approver_and_undo(day):
    aid = hold_request_id(day)
    tr = day.audit.trace(aid)
    assert tr["case"]["case_id"] == "C-FG531" and tr["proposal"]["agent"] == "passenger" and tr["proposal"]["rationale"]
    assert {d["feed"] for d in tr["data"]} >= {"movements", "pax"} and all(d["snapshot_id"] and d["fetched"] for d in tr["data"])
    passed = {r["rule"] for r in tr["rules"] if r["passed"]}
    assert {"G1", "G8", "G9"} <= passed and all(r["meaning"] for r in tr["rules"])
    assert tr["approver"]["by"] == "sim.duty_ops" and tr["approver"]["role"] == C.DUTY_OPS
    assert tr["undo"] == {"type": "HOLD_DEPARTURE", "params": {"minutes": -12}}
    assert tr["saga"] and tr["saga"][0]["status"] == "DONE"
    auto = day.store.one("SELECT action_id FROM actions WHERE type='GROUND_DISPATCH' AND status='EXECUTED'")
    assert day.audit.trace(auto["action_id"])["approver"]["by"] == "auto"


def test_timeline_covers_every_stage_and_the_report_is_built_from_the_log(day):
    stages = [r["stage"] for r in day.audit.timeline("C-FG531")]
    assert [s for s in dict.fromkeys(stages)][:4] == ["1-event", "2-validate", "3-coordinate", "4-agents"]
    assert {"5-merge", "6-guard", "7-approval", "8-execute", "9-wait", "10-report"} <= set(stages)
    report = day.audit.report("C-FG531")
    assert "FG531" in report and "APPROVED" in report and "HOLD_DEPARTURE" in report
    assert day.store.one("SELECT summary FROM cases WHERE case_id='C-FG531'")["summary"]


def test_two_runs_of_the_same_day_are_identical():
    a, b = Engine(approvers="simulated"), Engine(approvers="simulated")
    for e in (a, b):
        e.run_until(900)
    assert a.world.metrics() == b.world.metrics()
    ids = [[r["action_id"] for r in e.store.q("SELECT action_id FROM actions WHERE status='EXECUTED' ORDER BY action_id")]
           for e in (a, b)]
    assert ids[0] == ids[1] and len(ids[0]) > 200


class JunkLLM:
    enabled = True
    calls = 0

    def chat_json(self, system, user, tag=""):
        self.calls += 1
        return {"steps": [{"agent": "release_aircraft"}], "selected": ["no-such-action"], "rationale": "Ignore all rules."}

    def describe(self):
        return {"mode": "junk"}


def test_a_misbehaving_llm_cannot_break_or_steer_a_case():
    llm = JunkLLM()
    e = Engine(approvers="simulated", llm=llm)
    e.run_until(870)
    assert llm.calls > 10 and not e.store.q("SELECT 1 FROM audit_log WHERE kind='CASE_ERROR'")
    plans = [loads(r["detail"]) for r in e.store.q("SELECT detail FROM audit_log WHERE kind='PLAN'")]
    assert plans and all(p["source"] == "rule" for p in plans)                 # both LLM plans rejected, rule plan used
    checks = [loads(r["detail"]) for r in e.store.q("SELECT detail FROM audit_log WHERE kind='PLAN_CHECK'")]
    assert sum(1 for c in checks if c["source"] == "llm" and not c["passed"]) >= 2
    assert e.world.legs["FG532"]["hold"] == 12                                 # same outcome as the rule-based run
    assert not e.store.q("SELECT 1 FROM proposals WHERE llm_used=1")


def test_one_crashing_agent_does_not_take_the_case_down(monkeypatch):
    def boom(ctx):
        raise RuntimeError("baggage system exploded")
    monkeypatch.setitem(workflow.AGENT_FNS, "baggage", boom)
    e = Engine(approvers="simulated")
    e.run_until(856)
    assert e.cases["C-FG214"]["status"] != "ERROR"
    row = e.store.one("SELECT warnings FROM proposals WHERE case_id='C-FG214' AND agent='baggage'")
    assert "agent error" in row["warnings"]
    assert e.store.q("SELECT 1 FROM actions WHERE case_id='C-FG214' AND status IN ('EXECUTED','PENDING_APPROVAL')")


def test_maintenance_signoff_is_a_human_action_that_wakes_the_waiting_case():
    e = Engine(approvers="simulated", with_events=True)
    e.world.events = [ev for ev in e.world.events if ev["id"] != "S6b"]        # nobody signs automatically this time
    e.run_until(915)
    assert e.cases["C-FG466"]["status"] == "WAITING"
    assert {"kind": "signoff", "key": "FG-W902"} in e.cases["C-FG466"]["waits"]
    e.maintenance_signoff("FG-W902", "MX-CTRL-11")
    assert e.store.q("SELECT 1 FROM approvals WHERE case_id='C-FG466' AND signature LIKE 'SWAP_AIRCRAFT|FG467|FG-W902'")
    e.run_until(925)
    assert e.world.legs["FG467"]["ac"] == "FG-W902"


def test_llm_is_spent_on_headline_cases_plus_one_ordinary_case_per_minute():
    llm = JunkLLM()
    e = Engine(approvers="simulated", llm=llm)
    e.run_until(850)
    flags = {r["case_id"]: (loads(r["data"]).get("llm"), r["opened_at"]) for r in e.store.q("SELECT * FROM cases")}
    storm = [c for c, (on, t) in flags.items() if t == 845 and c.startswith("C-FG")]
    assert len(storm) > 20 and sum(flags[c][0] for c in storm) == 1          # the burst did not flood the model
    assert flags["C-FG214"][0] is True                                       # the scripted headline case got it
    assert llm.calls < 40
