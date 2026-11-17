"""Stage 3 — Coordinator agent (LLM supervisor) + the plan check in code.

The coordinator reads the case and plans WHICH agents run and IN WHAT ORDER. Code then checks the plan
with graphlib.TopologicalSorter: only known agents, no cycles, required agents present, required
dependencies respected (crew legality and maintenance status before any crew/aircraft option, the
re-accommodation solver only after the schedule option is fixed, ...).
Rejected plan -> sent back ONCE with the error -> if it fails again the rule plan is used. All logged.
"""
from __future__ import annotations

import json
from graphlib import CycleError, TopologicalSorter

from .models import PlanStep

AGENTS = {
    "propagation": "solver (no LLM): knock-on delay down the aircraft rotation and crew pairings",
    "crew_legality": "solver (no LLM): duty/rest status of the crew, legal standby options only",
    "maintenance_check": "tool (no LLM): reads maintenance sign-off for the aircraft and the spares",
    "turnaround": "LLM agent: turnaround tasks, off-block prediction, ground-resource dispatch",
    "passenger": "LLM agent: connections at risk and hold options (phase A); vouchers, hotels, messages (phase B)",
    "baggage": "LLM agent: bags at risk, hot transfers, priority tags, bags follow rebooked passengers",
    "crew_aircraft": "LLM agent: explains crew-swap and aircraft-swap options from the two checks above",
    "stands": "solver (no LLM): CP-SAT stand re-allocation",
    "reaccommodation": "solver (no LLM): CP-SAT passenger re-accommodation with the fairness priority table",
}
# agent -> agents that must (transitively) run before it whenever it is in the plan
MUST_FOLLOW = {"crew_aircraft": ["crew_legality", "maintenance_check"]}
# (later, earlier): if both are in the plan, `later` must come after `earlier`
ORDER_IF_BOTH = [("passenger", "reaccommodation"), ("baggage", "reaccommodation"), ("stands", "crew_aircraft"),
                 ("crew_legality", "propagation"), ("turnaround", "propagation"), ("reaccommodation", "propagation")]
RULE_PLANS = {
    ("LATE_INBOUND", "A"): {"propagation": [], "maintenance_check": [], "crew_legality": ["propagation"],
                            "turnaround": ["propagation"], "passenger": ["propagation"], "baggage": ["propagation"],
                            "crew_aircraft": ["crew_legality", "maintenance_check", "propagation"]},
    ("LATE_INBOUND", "B"): {"propagation": [], "reaccommodation": ["propagation"], "passenger": ["reaccommodation"],
                            "baggage": ["reaccommodation"]},
    ("CANCELLATION", "B"): {"reaccommodation": [], "passenger": ["reaccommodation"], "baggage": ["reaccommodation"]},
    ("AIRCRAFT_TECH", "A"): {"propagation": [], "maintenance_check": [], "crew_legality": ["propagation"],
                             "crew_aircraft": ["crew_legality", "maintenance_check", "propagation"]},
    ("STAND_CONFLICT", "A"): {"stands": []},
}
REQUIRED = {
    ("LATE_INBOUND", "A"): {"propagation", "crew_legality", "maintenance_check", "turnaround", "passenger", "crew_aircraft"},
    ("LATE_INBOUND", "B"): {"reaccommodation", "passenger"},
    ("CANCELLATION", "B"): {"reaccommodation", "passenger"},
    ("AIRCRAFT_TECH", "A"): {"maintenance_check", "crew_legality", "crew_aircraft"},
    ("STAND_CONFLICT", "A"): {"stands"},
}
SYSTEM = (
    "You are the coordinator of an airline disruption-recovery system. You only PLAN: decide which specialist "
    "agents run for this case and in what order. You never decide safety and you never take actions. "
    "Use only the agents listed. Rules: crew_aircraft must come after crew_legality and maintenance_check; "
    "reaccommodation is only allowed when the schedule is fixed (phase B) and passenger and baggage must then "
    "come after it; no cycles. Reply with JSON only: "
    '{"steps": [{"agent": "<name>", "after": ["<agent that must finish first>"]}], "reason": "<one sentence>"}'
)


def open_case(case_type: str, flight: str, hub, suffix: str = "") -> dict:
    """The shared case record every stage works on."""
    sched = hub.get("schedule", flight, snapshot=False).value or {}
    inbound = departure = None
    if case_type == "LATE_INBOUND":
        inbound, departure = flight, sched.get("next")
    elif case_type in ("CANCELLATION", "AIRCRAFT_TECH"):
        departure = flight
    return {"case_id": f"C-{flight}{suffix}", "type": case_type, "flight": flight, "inbound": inbound,
            "departure": departure, "phase": "B" if case_type == "CANCELLATION" else "A", "iteration": 1,
            "opened_at": hub.world.now, "decisions": {}, "title": "", "aircraft": sched.get("ac"),
            "dest": sched.get("dest")}


def rule_plan(case: dict) -> list[PlanStep]:
    plan = RULE_PLANS.get((case["type"], case["phase"])) or RULE_PLANS[(case["type"], "A" if case["phase"] == "B" else "B")]
    return [PlanStep(agent=a, after=list(deps)) for a, deps in plan.items()]


def check_plan(steps: list[PlanStep], case: dict) -> tuple[bool, str, list[list[str]]]:
    """Returns (ok, error, waves). `waves` are groups of agents that can run in parallel, in order."""
    names = [s.agent for s in steps]
    if not names:
        return False, "the plan is empty", []
    unknown = [n for n in names if n not in AGENTS]
    if unknown:
        return False, f"unknown agent {unknown[0]!r}", []
    if len(set(names)) != len(names):
        return False, "an agent appears twice", []
    graph = {s.agent: set(s.after) for s in steps}
    for agent, deps in graph.items():
        missing = [d for d in deps if d not in graph]
        if missing:
            return False, f"{agent} depends on {missing[0]!r}, which is not in the plan", []
    try:
        ts = TopologicalSorter(graph)
        ts.prepare()
    except CycleError as e:
        return False, f"the plan has a cycle: {' -> '.join(map(str, e.args[1]))}", []
    waves = []
    while ts.is_active():
        ready = sorted(ts.get_ready())
        waves.append(ready)
        ts.done(*ready)
    position = {a: i for i, w in enumerate(waves) for a in w}
    key = (case["type"], case["phase"])
    missing = sorted(REQUIRED.get(key, set()) - set(names))
    if missing:
        return False, f"required agent {missing[0]!r} is missing for a {case['type']} case in phase {case['phase']}", []
    if "reaccommodation" in names and case["phase"] != "B":
        return False, "reaccommodation may only run after the schedule option is fixed (phase B)", []
    for agent, before in MUST_FOLLOW.items():
        if agent in names:
            for b in before:
                if b not in names:
                    return False, f"{agent} needs {b} in the plan", []
                if position[b] >= position[agent]:
                    return False, f"{b} must finish before {agent}", []
    for later, earlier in ORDER_IF_BOTH:
        if later in names and earlier in names and position[earlier] >= position[later]:
            return False, f"{earlier} must finish before {later}", []
    return True, "", waves


def _parse(out: dict | None) -> tuple[list[PlanStep] | None, str]:
    if not isinstance(out, dict) or not isinstance(out.get("steps"), list):
        return None, "reply must be a JSON object with a 'steps' list"
    steps = []
    for s in out["steps"]:
        if not isinstance(s, dict) or not isinstance(s.get("agent"), str):
            return None, "every step needs an 'agent' name"
        after = s.get("after") or []
        if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
            return None, "'after' must be a list of agent names"
        steps.append(PlanStep(agent=s["agent"], after=after))
    return steps, ""


def plan_case(case: dict, llm=None, facts: dict | None = None) -> dict:
    """LLM plan -> code check -> (one replan with the error) -> rule plan. Every attempt is recorded."""
    attempts, error = [], ""
    if llm is not None and llm.enabled:
        user = {"case": {k: case.get(k) for k in ("case_id", "type", "flight", "inbound", "departure", "phase")},
                "facts": facts or {}, "agents": AGENTS}
        for n in (1, 2):
            if error:
                user["previous_plan_rejected_because"] = error
            out = llm.chat_json(SYSTEM, json.dumps(user, default=str), tag=f"{case['case_id']}:coordinator#{n}")
            if out is None:
                attempts.append({"source": "llm", "ok": False, "error": "LLM unavailable"})
                break
            steps, error = _parse(out)
            waves = []
            if steps is not None:
                ok, error, waves = check_plan(steps, case)
            attempts.append({"source": "llm", "ok": not error, "error": error,
                             "steps": [s.model_dump() for s in steps or []]})
            if not error:
                return {"source": "llm", "steps": [s.model_dump() for s in steps], "waves": waves,
                        "reason": str(out.get("reason", ""))[:300], "attempts": attempts}
    steps = rule_plan(case)
    ok, error, waves = check_plan(steps, case)
    assert ok, f"rule plan must always pass the check: {error}"
    attempts.append({"source": "rule", "ok": True, "error": ""})
    return {"source": "rule", "steps": [s.model_dump() for s in steps], "waves": waves,
            "reason": f"standard plan for a {case['type']} case, phase {case['phase']}", "attempts": attempts}
