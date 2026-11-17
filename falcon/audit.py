"""Stage 10 — audit log, case replay queries and the controller's delay report.

Every agent call, plan check, guard result, approval, saga step and undo is written here. For any
executed action `trace()` returns the full chain: case -> proposal -> data snapshots (with timestamps)
-> rules that passed -> approver (or "auto" + rule) -> undo. Passenger names and booking references are
never written: only hashed ids exist anywhere upstream of this module.
"""
from __future__ import annotations

from . import config as C
from .guards import RULES
from .store import Store, loads

STAGES = {1: "1-event", 2: "2-validate", 3: "3-coordinate", 4: "4-agents", 5: "5-merge", 6: "6-guard",
          7: "7-approval", 8: "8-execute", 9: "9-wait", 10: "10-report"}


class Audit:
    def __init__(self, store: Store, world):
        self.store, self.world = store, world

    def log(self, case_id: str, stage: int, kind: str, detail=None, actor: str = "system", action_id: str = "") -> None:
        self.store.add("audit_log", t=self.world.now, case_id=case_id, stage=STAGES[stage], kind=kind, actor=actor,
                       action_id=action_id, detail=detail if isinstance(detail, str) else (detail or {}))

    def timeline(self, case_id: str) -> list[dict]:
        """Step-by-step replay of one case."""
        rows = self.store.q("SELECT * FROM audit_log WHERE case_id=? ORDER BY id", case_id)
        for r in rows:
            r["detail"] = loads(r["detail"], r["detail"])
            r["time"] = C.hm(r["t"])
        return rows

    def trace(self, action_id: str) -> dict | None:
        """Everything behind one action: data, rules, approver and undo."""
        a = self.store.one("SELECT * FROM actions WHERE action_id=?", action_id)
        if not a:
            return None
        prop = self.store.one("SELECT * FROM proposals WHERE case_id=? AND agent=? AND iteration=? ORDER BY id DESC",
                              a["case_id"], a["agent"], a["iteration"]) or {}
        refs = loads(prop.get("data_refs"), [])
        wanted = set(loads(a["inputs"], []))
        used = [r for r in refs if f"{r['feed']}:{r['record_id']}" in wanted] or refs
        appr = self.store.one("SELECT * FROM approvals WHERE case_id=? AND signature=? AND status='APPROVED' "
                              "ORDER BY decided_at DESC", a["case_id"], a["signature"])
        rules = loads(a["rules"], [])
        return {
            "action": {**a, "params": loads(a["params"], {}), "inputs": sorted(wanted)},
            "case": self.store.one("SELECT case_id, type, flight, title, status FROM cases WHERE case_id=?", a["case_id"]),
            "proposal": {"agent": a["agent"], "rationale": prop.get("rationale", ""), "confidence": prop.get("confidence"),
                         "llm_used": bool(prop.get("llm_used"))},
            "data": [{**r, "fetched": C.hm(r["fetched_at"])} for r in used],
            "rules": [{**r, "meaning": RULES.get(r["rule"], "")} for r in rules],
            "approver": ({"by": appr["decided_by"], "role": appr["role"], "at": C.hm(appr["decided_at"]), "note": appr["note"]}
                         if appr else {"by": "auto", "role": None,
                                       "rule": "autonomous action: every guard rule passed, no human gate required"}),
            "saga": self.store.q("SELECT bundle, step, seq, status, t, detail FROM saga_steps WHERE action_id=? ORDER BY id",
                                 action_id),
            "undo": loads(a["undo"], None),
        }

    def report(self, case_id: str, llm=None) -> str:
        """The controller's delay report, drafted from the log (rule template; an LLM may only rephrase it)."""
        case = self.store.one("SELECT * FROM cases WHERE case_id=?", case_id)
        if not case:
            return ""
        acts = self.store.q("SELECT * FROM actions WHERE case_id=? ORDER BY t, action_id", case_id)
        done = [a for a in acts if a["status"] == "EXECUTED"]
        blocked = [a for a in acts if a["status"] in ("BLOCKED", "HELD")]
        appr = self.store.q("SELECT * FROM approvals WHERE case_id=? AND status!='NOTICE' ORDER BY requested_at", case_id)
        notices = self.store.q("SELECT * FROM approvals WHERE case_id=? AND status IN ('NOTICE','ACK')", case_id)
        by_type: dict[str, int] = {}
        for a in done:
            by_type[a["type"]] = by_type.get(a["type"], 0) + 1
        lines = [f"Delay report - case {case_id} ({case['type']}, flight {case['flight']})",
                 f"Opened {C.hm(case['opened_at'])}" + (f", closed {C.hm(case['closed_at'])}" if case["closed_at"] else ", still open")
                 + f". {case['title']}",
                 f"Executed actions: {len(done)} (" + (", ".join(f"{n} x {t}" for t, n in sorted(by_type.items())) or "none") + ")."]
        for r in appr:
            lines.append(f"Approval - {C.ROLE_NAMES.get(r['role'], r['role'])}: {r['summary']} -> {r['status']}"
                         + (f" by {r['decided_by']} at {C.hm(r['decided_at'])}" if r["decided_by"] else "")
                         + (" (safe default: current plan kept, escalated)" if r["status"] == "TIMEOUT" else ""))
        for a in blocked:
            rules = ", ".join(sorted({r["rule"] for r in loads(a["rules"], []) if not r.get("passed", True)}))
            lines.append(f"Held/blocked by guard {rules}: {a['type']} {a['target']}")
        for n in notices:
            lines.append(f"Escalated to {C.ROLE_NAMES.get(n['role'], n['role'])}: {n['summary']}")
        undone = self.store.q("SELECT COUNT(*) AS n FROM saga_steps WHERE case_id=? AND status='UNDONE'", case_id)[0]["n"]
        if undone:
            lines.append(f"Saga: {undone} step(s) were undone after a failed step; the case was re-planned on fresh data.")
        text = "\n".join(lines)
        if llm is not None and llm.enabled:
            out = llm.chat_json(
                "You rewrite an operations delay report for a duty controller in plain English. Keep every flight "
                "number, time and count exactly as given. Add nothing. Reply with JSON only: {\"report\": \"...\"}",
                text, tag=f"{case_id}:reporter")
            draft = (out or {}).get("report") if isinstance(out, dict) else None
            if isinstance(draft, str) and case["flight"] in draft and len(draft) < 4 * len(text):
                return draft.strip()
        return text
