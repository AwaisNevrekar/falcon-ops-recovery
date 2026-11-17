"""Stage 7 — approval routing: role-based sign-off, timeouts, safe defaults, escalation.

One approval record per action (never a blanket approval). No reply before the deadline -> status TIMEOUT,
the action is NOT executed (safe default: keep the current plan), and a new request goes to the escalation
role. Notices (escalations that need no decision) live in the same inbox with status NOTICE.
"""
from __future__ import annotations

import hashlib

from . import config as C
from .guards import approval_role
from .models import Action
from .store import Store


class Approvals:
    def __init__(self, store: Store, world):
        self.store, self.world = store, world

    @staticmethod
    def key(case_id: str, signature: str, role: str) -> str:
        return f"{case_id}|{signature}|{role}"

    def request(self, case_id: str, a: Action, role: str | None = None, escalated_from: str = "") -> str:
        """Create (or find) the pending request for this action. Idempotent across re-plans."""
        role = role or approval_role(a)
        aid = self.key(case_id, a.signature(), role)
        if not self.store.one("SELECT approval_id FROM approvals WHERE approval_id=?", aid):
            now = self.world.now
            self.store.add("approvals", approval_id=aid, case_id=case_id, action_id=a.action_id, signature=a.signature(),
                           role=role, summary=a.why, detail={"type": a.type, "target": a.target, "params": a.params,
                                                             "category": a.category, "agent": a.agent},
                           requested_at=now, deadline=now + C.APPROVAL_TIMEOUT_MIN[role], status="PENDING",
                           escalated_from=escalated_from)
        return aid

    def notice(self, case_id: str, role: str, text: str, action_id: str = "") -> str:
        aid = f"{case_id}|NOTICE|{role}|{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"
        self.store.add("approvals", approval_id=aid, case_id=case_id, action_id=action_id, signature="NOTICE", role=role,
                       summary=text, detail={}, requested_at=self.world.now, deadline=None, status="NOTICE")
        return aid

    def get(self, approval_id: str) -> dict | None:
        return self.store.one("SELECT * FROM approvals WHERE approval_id=?", approval_id)

    def decide(self, approval_id: str, decision: str, who: str, note: str = "") -> dict:
        """A human (or the simulated approver standing in for one) answers. Only PENDING requests can be decided."""
        row = self.get(approval_id)
        if not row or row["status"] != "PENDING":
            raise ValueError(f"approval {approval_id} is not pending")
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError("decision must be APPROVED or REJECTED")
        if not who:
            raise ValueError("an approval needs a named approver")
        self.store.set("approvals", "approval_id", approval_id, status=decision, decided_by=who,
                       decided_at=self.world.now, note=note)
        return self.get(approval_id)

    def acknowledge(self, approval_id: str, who: str) -> None:
        self.store.set("approvals", "approval_id", approval_id, status="ACK", decided_by=who, decided_at=self.world.now)

    def expire(self) -> list[dict]:
        """Time out overdue requests and escalate each one to the next role. Returns the timed-out rows."""
        now, out = self.world.now, []
        for row in self.store.q("SELECT * FROM approvals WHERE status='PENDING' AND deadline<=?", now):
            self.store.set("approvals", "approval_id", row["approval_id"], status="TIMEOUT", decided_at=now,
                           note="no reply in time: safe default (keep current plan), escalated")
            nxt = C.ESCALATION_ROLE.get(row["role"])
            esc_id = ""
            if nxt:
                esc_id = self.key(row["case_id"], row["signature"], nxt)
                self.store.add("approvals", approval_id=esc_id, case_id=row["case_id"], action_id=row["action_id"],
                               signature=row["signature"], role=nxt, summary=row["summary"], detail=row["detail"],
                               requested_at=now, deadline=now + C.APPROVAL_TIMEOUT_MIN[nxt], status="PENDING",
                               escalated_from=row["approval_id"])
            out.append({**row, "escalation_id": esc_id, "escalated_to": nxt})
        return out

    def for_action(self, case_id: str, a: Action) -> dict | None:
        """The APPROVED record for this decision, if any (the guard checks the role)."""
        return self.store.one("SELECT * FROM approvals WHERE case_id=? AND signature=? AND status='APPROVED' "
                              "ORDER BY decided_at DESC", case_id, a.signature())

    def inbox(self, role: str | None = None, include_done: bool = False) -> list[dict]:
        sql = "SELECT * FROM approvals WHERE 1=1" + (" AND role=?" if role else "")
        sql += "" if include_done else " AND status IN ('PENDING','NOTICE')"
        return self.store.q(sql + " ORDER BY requested_at, approval_id", *([role] if role else []))


class SimulatedApprovers:
    """Stands in for the human controllers in automated runs (scenarios, evaluation, optional demo autopilot).

    Each role answers after its response time. Scripted silences ("no_reply" in an event) simulate a
    controller who does not answer. It only ever uses Approvals.decide(), exactly like the dashboard buttons.
    """

    def __init__(self, approvals: Approvals, events: list[dict]):
        self.approvals = approvals
        self.silent = [{"case_id": f"C-{e['flight']}", **e["no_reply"]} for e in events if e.get("no_reply")]

    def _is_silent(self, row: dict) -> bool:
        return any(row["case_id"].startswith(s["case_id"]) and row["role"] == s["role"]
                   and row["signature"].startswith(s["type"] + "|") for s in self.silent)

    def act(self) -> list[dict]:
        now, out = self.approvals.world.now, []
        for row in self.approvals.store.q("SELECT * FROM approvals WHERE status='PENDING' ORDER BY requested_at, approval_id"):
            if self._is_silent(row) or now < row["requested_at"] + C.SIM_APPROVER_DELAY_MIN[row["role"]]:
                continue
            out.append(self.approvals.decide(row["approval_id"], "APPROVED", f"sim.{row['role'].lower()}",
                                             "simulated approver"))
        return out
