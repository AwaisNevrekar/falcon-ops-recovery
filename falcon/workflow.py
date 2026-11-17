"""The LangGraph workflow (stages 1-10) and the engine that runs ONE graph thread per disruption case.

  1 event stream + minute scans (Engine.tick / _scan)      6 guard            (code only, never an LLM)
  2 validate inputs             (ingest)                    7 approval routing (7a auto / 7b interrupt())
  3 coordinator + plan check    (coordinate)                8 execute as a saga through the write gateway
  4 specialist agents, parallel (Send -> worker)            9 wait and resume  (interrupt() + SQLite checkpointer)
  5 merge and resolve           (merge)                    10 log and report

Each case has its own thread id, so a slow partner or a pending approval only ever pauses that case.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from . import config as C
from .agents import Ctx, Toolbox, baggage, crew_aircraft, passenger, turnaround
from .agents import proposal as make_proposal
from .approvals import Approvals, SimulatedApprovers
from .audit import Audit
from .coordinator import open_case, plan_case
from .feeds import FeedHub
from .feeds.validate import reconcile_eta
from .guards import approval_role, run_guards
from .llm import LLM
from .merge import _resource, merge
from .models import REF_RX, Action, signoff_valid
from .replay import OpsDay
from .saga import WriteGateway
from .solvers.workers import WORKERS
from .store import Store

AGENT_FNS = {**WORKERS, "turnaround": turnaround.run, "passenger": passenger.run, "baggage": baggage.run,
             "crew_aircraft": crew_aircraft.run}
RESET = "__reset__"
CASE_HORIZON_MIN = 240          # act on inbounds that are airborne or due within 4 h, not on far-future forecasts
MAX_ITERATIONS = 8


def _acc(old, new):
    """Reducer for parallel worker results; RESET starts a new planning round."""
    if new == RESET or new is None:
        return []
    return (old or []) + list(new)


def _uniq(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for w in items:
        k = tuple(sorted(w.items()))
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out


class CaseState(TypedDict, total=False):
    case: dict
    inputs: dict
    plan: dict
    todo: list
    wave: list
    proposals: Annotated[list, _acc]
    merged: dict
    guard: dict
    pending: list
    waits: list
    flags: dict
    status: str


class Engine:
    def __init__(self, db_path: str = ":memory:", seed: int = C.SEED, llm=None, approvers: str = "manual",
                 start: int = C.REPLAY_START, with_events: bool = True):
        self.world = OpsDay(seed, start, with_events)
        self.store = Store(db_path)
        self.hub = FeedHub(self.world, self.store)
        self.llm = llm if llm is not None else LLM.off()
        refs = {p["ref"] for p in self.world.pnrs.values()}
        self.llm.secret_check = lambda text: any(tok in refs for tok in REF_RX.findall(text))   # privacy safety net
        self.audit = Audit(self.store, self.world)
        self.approvals = Approvals(self.store, self.world)
        self.gateway = WriteGateway(self.world, self.hub, self.store, self.audit, self.approvals)
        self.sim = SimulatedApprovers(self.approvals, self.world.events) if approvers == "simulated" else None
        ckpt = ":memory:" if db_path == ":memory:" else db_path + ".ckpt.db"
        self.saver = SqliteSaver(sqlite3.connect(ckpt, check_same_thread=False))
        self.graph = self._build()
        self.cases: dict[str, dict] = {}                 # live index of every case
        self.index: dict[tuple, str] = {}                # (type, flight) -> latest case id
        self._count: dict[tuple, int] = defaultdict(int)
        self._stragglers: dict[str, tuple] = {}
        self._llm_per_tick: dict[int, int] = {}
        self._events_seen: set[str] = set()
        self._alerted: set[str] = set()
        self.stand_seen: set[tuple] = set()
        self.lock = threading.RLock()
        self._record_events()

    # ================================================================== graph
    def _build(self):
        g = StateGraph(CaseState)
        for name in ("ingest", "coordinate", "next_wave", "worker", "merge", "guard", "route", "execute", "approval",
                     "decide", "wait", "report"):
            g.add_node(name, getattr(self, "_n_" + name))
        g.add_edge(START, "ingest")
        g.add_edge("ingest", "coordinate")
        g.add_edge("coordinate", "next_wave")
        g.add_conditional_edges("next_wave", self._fan_out, ["worker", "merge"])
        g.add_edge("worker", "next_wave")
        g.add_edge("merge", "guard")
        g.add_edge("guard", "route")
        g.add_edge("route", "execute")
        g.add_conditional_edges("execute", self._after_execute, ["approval", "decide"])
        g.add_edge("approval", "execute")
        g.add_conditional_edges("decide", lambda s: s["flags"]["next"], ["ingest", "wait", "report"])
        g.add_edge("wait", "ingest")
        g.add_edge("report", END)
        return g.compile(checkpointer=self.saver)

    # ---- stage 2: validate inputs
    def _n_ingest(self, state: CaseState) -> dict:
        case = dict(state["case"])
        case["iteration"] = case.get("iteration", 0) + 1
        cid, hub, now = case["case_id"], self.hub, self.world.now
        x, n = case.get("inbound"), case.get("departure")
        decisions = dict(case.get("decisions", {}))
        for sig, st in list(decisions.items()):          # an escalation may have been answered while we slept
            if st == "TIMEOUT":
                rows = self.store.q("SELECT status FROM approvals WHERE case_id=? AND signature=?", cid, sig)
                if any(r["status"] == "APPROVED" for r in rows):
                    decisions[sig] = "APPROVED"
                elif any(r["status"] == "REJECTED" for r in rows):
                    decisions[sig] = "REJECTED"
        case["decisions"] = decisions
        inputs: dict = {"t": now}
        if x:
            mov, atc = hub.get("movements", x), hub.get("atc", x)
            rec = reconcile_eta(mov, atc)
            sta = (mov.value or atc.value or {}).get("sta")
            delay = max(0, rec["eta"] - sta) if rec["eta"] is not None and sta is not None else 0
            inputs["eta"] = {**rec, "delay_min": delay, "snapshots": [mov.snapshot_id, atc.snapshot_id]}
            case["handled_delay"] = delay
            origin = (hub.get("schedule", x, snapshot=False).value or {}).get("origin", "")
            case["title"] = (f"Inbound {x} from {origin} is {delay} min late (ETA {C.hm(rec['eta'])})"
                             + (f"; next departure {n}" if n else "") + (f" - data {rec['status']}" if rec["status"] != "OK" else ""))
        elif case["type"] == "CANCELLATION":
            pax = sum(g["size"] for g in hub.get("pax", "orig:" + case["flight"], snapshot=False).value or [])
            case["title"] = f"{case['flight']} to {case.get('dest')} is cancelled: {pax} passengers to re-accommodate"
        elif case["type"] == "AIRCRAFT_TECH":
            case["title"] = f"{case.get('aircraft')} has no maintenance sign-off; {case['flight']} cannot depart on it"
        elif case["type"] == "STAND_CONFLICT":
            conf = hub.get("stands", "conflicts").value or []
            case["title"] = f"{len(conf)} stand double allocation(s) in the next {C.STAND_HORIZON_MIN} min"
        if n:
            hub.get("turnaround", n)                      # stage-2 scan of free-text ops notes happens in the adapter
            blocked = hub["turnaround"].blocked.get(n)
            if blocked:
                inputs["blocked_notes"] = blocked
                if n not in self._alerted:
                    self._alerted.add(n)
                    self.audit.log(cid, 2, "INJECTION_BLOCKED", {"flight": n, **blocked})
                    self.approvals.notice(cid, C.DUTY_OPS, f"Security: a free-text ops note for {n} looked like a prompt "
                                          f"injection ({', '.join(blocked['patterns'])}) and was blocked at input validation. "
                                          "No agent saw the text and no action was taken on it.")
        inputs["feeds_not_ok"] = [{"feed": r["feed"], "status": r["status"], "flagged": r["records_flagged"]}
                                  for r in hub.health() if r["status"] != "OK"]
        self.audit.log(cid, 2, "VALIDATED", {"iteration": case["iteration"], "phase": case["phase"], **inputs})
        self.store.set("cases", "case_id", cid, title=case["title"], iteration=case["iteration"])
        return {"case": case, "inputs": inputs, "proposals": RESET, "merged": {}, "guard": {}, "pending": [],
                "waits": [], "flags": {}}

    # ---- stage 3: coordinator + plan check
    def _n_coordinate(self, state: CaseState) -> dict:
        case = state["case"]
        facts = {"title": case["title"], "eta_status": (state["inputs"].get("eta") or {}).get("status"),
                 "feeds_not_ok": [f["feed"] for f in state["inputs"]["feeds_not_ok"]]}
        plan = plan_case(case, self.llm if case.get("llm") else None, facts)
        for i, att in enumerate(plan["attempts"], 1):
            self.audit.log(case["case_id"], 3, "PLAN_CHECK", {"attempt": i, "source": att["source"], "passed": att["ok"],
                                                             "error": att["error"]}, actor="coordinator")
        self.audit.log(case["case_id"], 3, "PLAN", {"source": plan["source"], "waves": plan["waves"],
                                                   "reason": plan["reason"]}, actor="coordinator")
        return {"plan": plan, "todo": plan["waves"]}

    # ---- stage 4: specialist agents and solvers, in parallel where the plan allows
    def _n_next_wave(self, state: CaseState) -> dict:
        todo = state.get("todo") or []
        return {"wave": todo[0] if todo else [], "todo": todo[1:]}

    def _fan_out(self, state: CaseState):
        if not state.get("wave"):
            return "merge"
        results = {p["agent"]: p for p in state.get("proposals") or []}
        return [Send("worker", {"agent": a, "case": state["case"], "results": results}) for a in state["wave"]]

    def _n_worker(self, payload: dict) -> dict:
        name, case = payload["agent"], payload["case"]
        ctx = Ctx(case=case, tb=Toolbox(self.hub), results=payload["results"], llm=self.llm if case.get("llm") else None)
        try:
            p = AGENT_FNS[name](ctx)
        except Exception as e:                            # one failing agent never takes the case down
            p = make_proposal(ctx, name, [], f"{name} failed ({type(e).__name__}); its part of the plan is skipped.",
                              warnings=[f"agent error: {e}"], confidence=0.0)
        d = p.model_dump()
        self.store.add("proposals", case_id=case["case_id"], iteration=case["iteration"], agent=name, t=self.world.now,
                       rationale=p.rationale, confidence=p.confidence, llm_used=p.llm_used, data_refs=d["data_refs"],
                       warnings=p.warnings, assumptions=p.assumptions, extras=d["extras"])
        self.audit.log(case["case_id"], 4, "PROPOSAL", {
            "agent": name, "actions": len(p.actions), "llm_used": p.llm_used, "confidence": p.confidence,
            "rationale": p.rationale, "warnings": p.warnings,
            "tool_calls": [f"{r.feed}:{r.record_id} [{r.status}, {r.age_s}s old]" for r in p.data_refs][:30]}, actor=name)
        return {"proposals": [d]}

    # ---- stage 5: merge and resolve
    def _n_merge(self, state: CaseState) -> dict:
        case = state["case"]
        cid, it, now = case["case_id"], case["iteration"], self.world.now

        def seat_free(fid, cabin):
            return (self.hub.get("inventory", fid, snapshot=False).value or {}).get("free_" + cabin.lower(), 0)

        m = merge(state.get("proposals") or [], case, self.world.reserved, seat_free)
        for a in m["actions"]:
            self.store.add("actions", replace=True, action_id=a["action_id"], case_id=cid, iteration=it, agent=a["agent"],
                           type=a["type"], category=a["category"], target=a["target"], params=a["params"], why=a["why"],
                           bundle=a["bundle"], requires=a["requires"], inputs=a["inputs"],
                           signature=Action.model_validate(a).signature(), status="PROPOSED", undo=a["undo"],
                           est_cost=a["est_cost_delta"], est_delay=a["est_delay_delta"], t=now)
        for d in m["dropped"]:
            self.store.add("actions", replace=True, action_id=d["action_id"], case_id=cid, iteration=it, type=d["type"],
                           target=d["target"], why=d["reason"], status="DROPPED", t=now)
        self.audit.log(cid, 5, "MERGED", {"kept": len(m["actions"]), "dropped": len(m["dropped"]),
                                          "collisions": m["collisions"], "score": m["score"], "tradeoffs": m["tradeoffs"]})
        return {"merged": m}

    # ---- stage 6: guard (code only)
    def _n_guard(self, state: CaseState) -> dict:
        case = state["case"]
        cid = case["case_id"]
        ctx = self.gateway.ctx(cid)
        actions = [Action.model_validate(a) for a in state["merged"]["actions"]]
        out = run_guards(actions, ctx, "plan")
        by_action = defaultdict(list)
        for r in out["results"]:
            by_action[r.action_id].append(r.model_dump())
        waits, removed = [], []
        for a in out["kept"]:
            self.store.set("actions", "action_id", a.action_id, rules=by_action[a.action_id])
        for a, fails in out["removed"]:
            data_only = all(f.rule in ("G8", "DEP") for f in fails)
            status = "HELD" if data_only else "BLOCKED"
            self.store.set("actions", "action_id", a.action_id, status=status, rules=by_action[a.action_id])
            removed.append({"action_id": a.action_id, "type": a.type, "target": a.target, "status": status,
                            "category": a.category, "rules": [f.model_dump() for f in fails]})
            self.audit.log(cid, 6, "GUARD_" + status, {"type": a.type, "target": a.target,
                                                      "failed": [f"{f.rule}: {f.detail}" for f in fails]},
                           action_id=a.action_id)
            if any(f.rule == "G8" for f in fails):
                waits += [{"kind": "feed", "ref": ref} for ref in a.inputs if ctx.input_status(ref) != "OK"]
            elif not data_only:                           # a hard rule failed: remove AND escalate, never approve away
                role = {"CREW": C.CREW_CTRL, "AIRCRAFT": C.MAINT}.get(a.category, C.DUTY_OPS)
                f = next(f for f in fails if f.rule != "DEP")
                self.approvals.notice(cid, role, f"Guard rule {f.rule} removed {a.type} for {a.target}: {f.detail}. "
                                      "This cannot be approved; a controller decision is needed.", a.action_id)
        waits = _uniq(waits)
        held = [r for r in removed if r["status"] == "HELD"]
        if held and waits:
            bad = ", ".join(f"{w['ref']} ({ctx.input_status(w['ref'])})" for w in waits)
            self.approvals.notice(cid, C.DUTY_OPS, f"{len(held)} action(s) for {case['flight']} are on hold because the "
                                  f"data is not reliable: {bad}. They resume automatically when the data is fresh and consistent.")
        self.audit.log(cid, 6, "GUARD", {"checked": len(actions), "kept": len(out["kept"]),
                                         "blocked": sum(r["status"] == "BLOCKED" for r in removed), "held": len(held),
                                         "rules_run": sorted({r.rule for r in out["results"]})})
        return {"guard": {"kept": [a.model_dump() for a in out["kept"]], "removed": removed}, "waits": waits}

    # ---- stage 7: approval routing (7a automatic, 7b role-based sign-off)
    def _n_route(self, state: CaseState) -> dict:
        cid, pending = state["case"]["case_id"], []
        for d in state["guard"]["kept"]:
            a = Action.model_validate(d)
            role = approval_role(a)
            if not role or self.approvals.for_action(cid, a):
                continue
            aid = self.approvals.request(cid, a)
            row = self.approvals.get(aid)
            if row["status"] != "PENDING":
                continue
            pending.append(aid)
            self.store.set("actions", "action_id", a.action_id, status="PENDING_APPROVAL")
            res = _resource(a)
            if res and res.split(":")[0] in ("crew", "aircraft"):
                self.world.reserved[res] = cid            # nobody else may plan with this crew member / aircraft
            self.audit.log(cid, 7, "APPROVAL_REQUESTED", {"role": role, "deadline": C.hm(row["deadline"]),
                                                         "summary": a.why}, action_id=a.action_id)
        return {"pending": pending}

    def _n_approval(self, state: CaseState) -> dict:
        pend = [i for i in state.get("pending") or [] if (self.approvals.get(i) or {}).get("status") == "PENDING"]
        if pend:
            interrupt({"kind": "approval", "pending": pend})          # the case sleeps here; others keep running
        return {}

    def _after_execute(self, state: CaseState) -> str:
        still = [i for i in state.get("pending") or [] if (self.approvals.get(i) or {}).get("status") == "PENDING"]
        return "approval" if still else "decide"

    # ---- stage 8: execute as a saga
    def _n_execute(self, state: CaseState) -> dict:
        case = dict(state["case"])
        cid, now = case["case_id"], self.world.now
        decisions = dict(case.get("decisions", {}))
        flags, waits = dict(state.get("flags") or {}), list(state.get("waits") or [])
        kept = [Action.model_validate(d) for d in state["guard"]["kept"]]
        status = {r["action_id"]: r["status"] for r in
                  self.store.q("SELECT action_id, status FROM actions WHERE case_id=? AND iteration=?", cid, case["iteration"])}
        approver: dict[str, str] = {}
        for a in kept:                                    # take in the human decisions made so far
            role, sig = approval_role(a), a.signature()
            if not role or status.get(a.action_id) not in ("PROPOSED", "PENDING_APPROVAL"):
                continue
            rows = self.store.q("SELECT * FROM approvals WHERE case_id=? AND signature=? AND status NOT IN "
                                "('NOTICE','ACK','WITHDRAWN') ORDER BY requested_at", cid, sig)
            states = {r["status"] for r in rows}
            ok = next((r for r in rows if r["status"] == "APPROVED"), None)
            if ok:
                decisions[sig] = "APPROVED"
                approver[a.action_id] = f"{ok['decided_by']} ({C.ROLE_NAMES[ok['role']]})"
            elif "REJECTED" in states:
                decisions[sig], status[a.action_id], flags["rejected"] = "REJECTED", "REJECTED", True
                self.store.set("actions", "action_id", a.action_id, status="REJECTED")
                self.world.reserved.pop(_resource(a) or "", None)
                self.audit.log(cid, 7, "REJECTED", {"type": a.type, "target": a.target,
                                                   "next": "re-plan without this option"}, action_id=a.action_id)
            elif "TIMEOUT" in states:
                decisions[sig], status[a.action_id] = "TIMEOUT", "TIMEOUT_SAFE_DEFAULT"
                self.store.set("actions", "action_id", a.action_id, status="TIMEOUT_SAFE_DEFAULT")
                esc = next((r for r in rows if r["status"] == "PENDING"), None)
                if esc:
                    waits.append({"kind": "approval", "id": esc["approval_id"]})
                else:
                    self.world.reserved.pop(_resource(a) or "", None)
                self.audit.log(cid, 7, "TIMEOUT_SAFE_DEFAULT", {
                    "type": a.type, "target": a.target, "safe_default": "keep the current plan; no crew, aircraft or "
                    "schedule change", "escalated_to": esc["role"] if esc else None}, action_id=a.action_id)
        for a in kept:                                    # children run under their parent's approval
            if a.requires in approver:
                approver.setdefault(a.action_id, approver[a.requires])
        done = {i for i, s in status.items() if s == "EXECUTED"}
        skipped = {i for i, s in status.items() if s in ("REJECTED", "TIMEOUT_SAFE_DEFAULT", "FAILED", "UNDONE", "NOT_RUN",
                                                         "DEP_SKIPPED", "BLOCKED", "HELD", "DROPPED")}
        ready: list[Action] = []
        for a in kept:                                    # parents come before their children in `kept`
            if status.get(a.action_id, "PROPOSED") not in ("PROPOSED", "PENDING_APPROVAL"):
                continue
            if approval_role(a) and decisions.get(a.signature()) != "APPROVED":
                continue
            if a.requires and a.requires in skipped:
                skipped.add(a.action_id)
                self.store.set("actions", "action_id", a.action_id, status="DEP_SKIPPED")
                continue
            if a.requires and a.requires not in done and a.requires not in {r.action_id for r in ready}:
                continue                                  # parent still waits for its approval
            ready.append(a)
        bundles: dict[str, list[Action]] = {}
        for a in ready:
            bundles.setdefault(a.bundle or a.action_id, []).append(a)
        for acts in bundles.values():
            if any(a.requires in skipped for a in acts):
                for a in acts:
                    skipped.add(a.action_id)
                    self.store.set("actions", "action_id", a.action_id, status="DEP_SKIPPED")
                continue
            res = self.gateway.run_bundle(cid, acts)
            if res["ok"]:
                for a in acts:
                    who = approver.get(a.action_id, "auto")
                    self.store.set("actions", "action_id", a.action_id, status="EXECUTED", executed_at=now,
                                   decided_by=who, rules=res["rules"][a.action_id])
                    decisions[a.signature()] = "EXECUTED"
                    done.add(a.action_id)
                    self.audit.log(cid, 8, "EXECUTED", {
                        "type": a.type, "target": a.target, "approved_by": who, "inputs": a.inputs,
                        "rules_passed": sorted({r["rule"] for r in res["rules"][a.action_id] if r["passed"]}),
                        "undo": a.undo.model_dump() if a.undo else None}, actor=who, action_id=a.action_id)
                continue
            for a in acts:
                st = ("FAILED" if a.action_id == res["failed_action"] else "UNDONE" if a.action_id in res["undone"]
                      else "NOT_RUN")
                if st == "FAILED" and res["kind"] in ("partner", "data"):
                    st = "HELD"
                skipped.add(a.action_id)
                self.store.set("actions", "action_id", a.action_id, status=st, rules=res["rules"].get(a.action_id, []))
            self.audit.log(cid, 8, "SAGA_FAILED", {"bundle": res["bundle"], "failed_action": res["failed_action"],
                                                  "error": res["error"], "steps_done_then_undone": res["steps_done"],
                                                  "undone_actions": res["undone"], "kind": res["kind"]},
                           action_id=res["failed_action"])
            failed = next(a for a in acts if a.action_id == res["failed_action"])
            if res["kind"] == "partner":
                waits.append({"kind": "feed", "ref": "hotel:availability"})
                self.approvals.notice(cid, C.CS_LEAD, f"Hotel partner API is not answering: {failed.why} is queued for "
                                      f"{case['flight']}. Other flights are not affected; it resumes when the partner is back.")
            elif res["kind"] == "data":
                waits += [{"kind": "feed", "ref": r} for r in failed.inputs
                          if self.gateway.ctx(cid).input_status(r) != "OK"]
            else:
                flags["replan"] = True                    # live data changed between planning and writing
        case["decisions"] = decisions
        return {"case": case, "flags": flags, "waits": _uniq(waits)}

    # ---- stage 9: decide what is next, wait and resume
    def _n_decide(self, state: CaseState) -> dict:
        case = dict(state["case"])
        cid, flags = case["case_id"], dict(state.get("flags") or {})
        waits = list(state.get("waits") or []) + case.pop("carry", [])
        for p in state.get("proposals") or []:
            waits += p.get("extras", {}).get("waits", [])
        waits = _uniq(waits)
        nxt = "report"
        if case["iteration"] >= MAX_ITERATIONS:
            case["outcome"] = "handed to the controller after repeated re-planning"
            self.approvals.notice(cid, C.DUTY_OPS, f"Case {cid} was re-planned {case['iteration']} times without settling; "
                                  "please handle the rest manually.")
        elif flags.get("replan"):
            nxt = "ingest"                                # a saga failed on live data: plan again on fresh data
        elif case["phase"] == "A" and any(w["kind"] == "feed" for w in waits):
            nxt = "wait"                                  # unreliable data: hold, do not guess
        elif case["type"] == "LATE_INBOUND" and case["phase"] == "A":
            case["phase"], nxt = "B", "ingest"            # schedule option is now fixed -> re-accommodation
            case["carry"] = [w for w in waits if w["kind"] != "feed"]
        elif waits:
            nxt = "wait"
        self.audit.log(cid, 9, "NEXT", {"next": nxt, "phase": case["phase"], "waiting_for": waits if nxt == "wait" else []})
        return {"case": case, "waits": waits, "flags": {**flags, "next": nxt}}

    def _n_wait(self, state: CaseState) -> dict:
        interrupt({"kind": "wait", "waits": state.get("waits") or []})   # durable sleep in the checkpointer
        case = dict(state["case"])
        if case["type"] in ("LATE_INBOUND", "AIRCRAFT_TECH"):
            case["phase"] = "A"
        return {"case": case}

    # ---- stage 10: log and report
    def _n_report(self, state: CaseState) -> dict:
        case = state["case"]
        cid = case["case_id"]
        for res in [r for r, holder in self.world.reserved.items() if holder == cid]:
            del self.world.reserved[res]
        self.store.set("cases", "case_id", cid, status="CLOSED", closed_at=self.world.now, data=case)
        summary = self.audit.report(cid)
        self.store.set("cases", "case_id", cid, summary=summary)
        self.audit.log(cid, 10, "CLOSED", {"iterations": case["iteration"], "outcome": case.get("outcome", "handled")})
        return {"status": "CLOSED"}

    # ================================================================== engine
    def _record_events(self) -> None:
        for t, eid, title, shows in self.world.event_log:
            if eid not in self._events_seen:
                self._events_seen.add(eid)
                self.store.add("events", t=t, event_id=eid, kind="scripted", title=title, payload={"shows": shows})
                self.audit.log("", 1, "EVENT", {"id": eid, "title": title})

    def open(self, case_type: str, flight: str, delay: int = 0, phase: str | None = None) -> str:
        key = (case_type, flight)
        self._count[key] += 1
        n = self._count[key]
        case = open_case(case_type, flight, self.hub, suffix="" if n == 1 else f"-{n}")
        case["iteration"] = 0
        if phase:
            case["phase"] = phase
        cid, now = case["case_id"], self.world.now
        # LLM budget: the scripted headline cases plus one ordinary case per minute get the LLM; the rest use the
        # rule fallbacks, so a burst of 30 cases in one minute never stalls on (or floods) the model provider.
        headline = bool(self.world.legs.get(flight, {}).get("story")) or case_type in ("CANCELLATION", "AIRCRAFT_TECH")
        used = self._llm_per_tick.get(now, 0)
        case["llm"] = bool(self.llm.enabled and (headline or used < 1))
        if case["llm"] and not headline:
            self._llm_per_tick[now] = used + 1
        self.store.add("cases", case_id=cid, type=case_type, flight=flight, title="", status="RUNNING", opened_at=now, data=case)
        self.cases[cid] = {"case_id": cid, "type": case_type, "flight": flight, "status": "RUNNING", "handled": delay,
                           "opened_at": now, "waits": [], "pending": [], "title": "", "phase": case["phase"]}
        self.index[key] = cid
        self.audit.log(cid, 1, "CASE_OPENED", {"type": case_type, "flight": flight, "trigger_delay_min": delay})
        self._run(cid, {"case": case})
        return cid

    def _run(self, cid: str, inp) -> None:
        info = self.cases[cid]
        cfg = {"configurable": {"thread_id": cid}, "recursion_limit": 500}
        try:
            res = self.graph.invoke(inp, cfg)
        except Exception as e:                            # a broken case affects only itself
            info.update(status="ERROR", pending=[], waits=[])
            self.store.set("cases", "case_id", cid, status="ERROR")
            self.audit.log(cid, 10, "CASE_ERROR", {"error": f"{type(e).__name__}: {e}"})
            self.approvals.notice(cid, C.DUTY_OPS, f"Case {cid} stopped on an internal error ({type(e).__name__}). "
                                  "Nothing was executed after the error; please handle this flight manually.")
            return
        intr = res.get("__interrupt__")
        if intr:
            v = intr[0].value
            if v["kind"] == "approval":
                info.update(status="WAITING_APPROVAL", pending=v["pending"], waits=[])
            else:
                info.update(status="WAITING", waits=v["waits"], pending=[], waiting_since=self.world.now)
        else:
            info.update(status="CLOSED", pending=[], waits=[], closed_at=self.world.now)
        case = res.get("case") or {}
        info.update(title=case.get("title", info["title"]), phase=case.get("phase", info["phase"]),
                    handled=max(info["handled"], case.get("handled_delay", 0)), iteration=case.get("iteration", 0))
        self.store.set("cases", "case_id", cid, status=info["status"], wait=info["waits"], title=info["title"])

    def _wake_reason(self, info: dict) -> str | None:
        now, ctx = self.world.now, self.gateway.ctx(info["case_id"])
        for w in info["waits"]:
            if w["kind"] == "feed" and ctx.input_status(w["ref"]) == "OK":
                return f"{w['ref']} is fresh and consistent again"
            if w["kind"] == "signoff":
                ac = self.hub.get("maintenance", w["key"], snapshot=False).value
                if signoff_valid(ac, now):
                    return f"Maintenance Control recorded a sign-off for {w['key']}"
            if w["kind"] == "approval":
                row = self.approvals.get(w["id"])
                if row and row["status"] != "PENDING":
                    return f"escalation answered: {row['status']}"
        if now - info.get("waiting_since", now) >= 120:
            return "wait limit reached"
        return None

    def _resume_ready(self) -> None:
        for cid, info in list(self.cases.items()):
            if info["status"] == "WAITING_APPROVAL":
                if any((self.approvals.get(i) or {}).get("status") != "PENDING" for i in info["pending"]):
                    self._run(cid, Command(resume="decision"))
            elif info["status"] == "WAITING":
                why = self._wake_reason(info)
                if why:
                    self.audit.log(cid, 9, "RESUMED", {"why": why})
                    self._run(cid, Command(resume=why))

    def _maybe_open(self, case_type: str, flight: str, delay: int, straggler: bool = False) -> None:
        cid = self.index.get((case_type, flight))
        info = self.cases.get(cid) if cid else None
        if info is None:
            self.open(case_type, flight, delay, phase="B" if straggler else None)
        elif info["status"] == "WAITING" and delay >= info["handled"] + C.CASE_REOPEN_MIN:
            self.audit.log(cid, 9, "RESUMED", {"why": f"delay grew to {delay} min"})
            self._run(cid, Command(resume="new ETA"))
        elif info["status"] in ("CLOSED", "ERROR"):
            if straggler:
                n, last = self._stragglers.get(flight, (0, -99))
                if n >= 3 or self.world.now - last < 15:
                    return
                self._stragglers[flight] = (n + 1, self.world.now)
                self.open(case_type, flight, delay, phase="B")
            elif delay >= info["handled"] + C.CASE_REOPEN_MIN:
                self.open(case_type, flight, delay)

    def _scan(self) -> None:
        """Stage 1: the minute scan that turns feed changes into disruption cases."""
        now, hub = self.world.now, self.hub
        late, meta, cancelled = {}, {}, []
        for r in hub.get("movements", "*", snapshot=False).value or []:
            if r["status"] == "CANCELLED":
                if r["kind"] == "DEP":
                    cancelled.append(r["fid"])
            elif r["kind"] == "ARR":
                late[r["fid"]], meta[r["fid"]] = r["arr_delay"], r
        for r in hub.get("atc", "*", snapshot=False).value or []:
            late[r["fid"]] = max(late.get(r["fid"], 0), r["arr_delay"])
            meta.setdefault(r["fid"], {"sta": r["sta"], "atd": None})
        for fid in sorted(late):
            m = meta[fid]
            if late[fid] >= C.LATE_INBOUND_MIN and (m.get("atd") is not None or m["sta"] - now <= CASE_HORIZON_MIN):
                self._maybe_open("LATE_INBOUND", fid, late[fid])
        risk = hub.get("pax", "at_risk", snapshot=False).value or {}
        for fid in sorted(risk):                          # small delays still get a case if passengers or bags are at risk
            r = risk[fid]
            if late.get(fid, 0) < C.LATE_INBOUND_MIN and (r["airborne"] or r["sta"] - now <= CASE_HORIZON_MIN):
                self._maybe_open("LATE_INBOUND", fid, max(r["delay"], late.get(fid, 0)))
        for fid in sorted(cancelled):
            self._maybe_open("CANCELLATION", fid, 0)
        for u in hub.get("maintenance", "unserviceable", snapshot=False).value or []:
            self._maybe_open("AIRCRAFT_TECH", u["flight"], 0)
        conflicts = hub.get("stands", "conflicts", snapshot=False).value or []
        keys = {(c["stand"], c["a"], c["b"]) for c in conflicts}
        if keys - self.stand_seen:
            self.stand_seen |= keys
            self.open("STAND_CONFLICT", f"STANDS-{C.hm(now).replace(':', '')}")
        for inb in sorted(hub.get("pax", "misconnected", snapshot=False).value or {}):
            self._maybe_open("LATE_INBOUND", inb, late.get(inb, 0), straggler=True)

    def tick(self, minutes: int = 1) -> int:
        """Advance the simulated clock: play events, answer/expire approvals, resume sleeping cases, scan."""
        with self.lock:
            for _ in range(minutes):
                self.world.step()
                self._record_events()
                if self.sim:
                    for row in self.sim.act():
                        self.audit.log(row["case_id"], 7, "APPROVAL_" + row["status"], {
                            "role": row["role"], "summary": row["summary"]}, actor=row["decided_by"], action_id=row["action_id"])
                for row in self.approvals.expire():
                    self.audit.log(row["case_id"], 7, "APPROVAL_TIMEOUT", {
                        "role": row["role"], "escalated_to": row["escalated_to"], "summary": row["summary"]},
                        action_id=row["action_id"])
                self._resume_ready()
                self._scan()
            return self.world.now

    def run_until(self, minute: int) -> int:
        while self.world.now < minute:
            self.tick()
        return self.world.now

    # ---- human actions (dashboard buttons call exactly these)
    def decide(self, approval_id: str, decision: str, who: str, note: str = "") -> dict:
        with self.lock:
            row = self.approvals.decide(approval_id, decision, who, note)
            self.audit.log(row["case_id"], 7, "APPROVAL_" + decision, {"role": row["role"], "summary": row["summary"],
                                                                      "note": note}, actor=who, action_id=row["action_id"])
            self._resume_ready()
            return row

    def maintenance_signoff(self, reg: str, who: str) -> None:
        """Maintenance Control (a human) records a release in the maintenance system. The system never does this."""
        with self.lock:
            self.world.record_signoff(reg, who)
            self.audit.log("", 1, "MAINTENANCE_SIGNOFF", {"aircraft": reg}, actor=who)
            self._resume_ready()

    def stats(self) -> dict:
        acts = self.store.q("SELECT status, decided_by FROM actions")
        done = [a for a in acts if a["status"] == "EXECUTED"]
        appr = self.store.q("SELECT * FROM approvals WHERE signature!='NOTICE'")
        waits = [r["decided_at"] - r["requested_at"] for r in appr if r["status"] in ("APPROVED", "REJECTED")]
        return {
            "cases": len(self.cases), "cases_open": sum(c["status"] != "CLOSED" for c in self.cases.values()),
            "actions_executed": len(done), "actions_auto": sum(a["decided_by"] == "auto" for a in done),
            "actions_approved": sum(a["decided_by"] != "auto" for a in done),
            "actions_blocked_by_guard": sum(a["status"] == "BLOCKED" for a in acts),
            "actions_held_bad_data": sum(a["status"] == "HELD" for a in acts),
            "escalations": self.store.one("SELECT COUNT(*) AS n FROM approvals WHERE signature='NOTICE'")["n"],
            "approvals_requested": len(appr), "approvals_timed_out": sum(r["status"] == "TIMEOUT" for r in appr),
            "approval_wait_avg_min": round(sum(waits) / len(waits), 1) if waits else 0.0,
            "saga_undos": self.store.one("SELECT COUNT(*) AS n FROM saga_steps WHERE status='UNDONE'")["n"],
            "llm_calls": getattr(self.llm, "calls", 0), "writes": self.gateway.writes,
        }
