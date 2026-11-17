"""Stage 8 — execute as a saga, through ONE write gateway.

`WriteGateway.run_bundle()` is the only code path that changes the systems of record. For every action it
  1. re-runs the guard rules on LIVE data (stage "execute": this is where the approval record is required),
  2. expands the action into small steps, each with a compensating undo,
  3. runs the steps; if any step fails, the steps already done are undone in reverse order.
There is no handler for releasing an aircraft or creating a maintenance sign-off: it cannot be done from here.
"""
from __future__ import annotations

from typing import Any, Callable

from . import config as C
from .agents.passenger import render
from .guards import GuardCtx, check_action
from .models import Action, FeedDown, WriteRejected

Step = tuple[str, Callable[[], Any], Callable[[Any], None]]


class WriteGateway:
    def __init__(self, world, hub, store, audit, approvals):
        self.world, self.hub, self.store, self.audit, self.approvals = world, hub, store, audit, approvals
        self._inflight: set[str] = set()
        self.writes = 0

    # ------------------------------------------------------------------ what each action does (and its undo)
    def steps(self, case_id: str, a: Action) -> list[Step]:
        w, p, t = self.world, a.params, a.target
        if a.type == "GROUND_DISPATCH":
            return [(f"dispatch {p['measure']} to {t}", lambda: w.dispatch(t, p["measure"]),
                     lambda _: w.dispatch(t, p["measure"], undo=True))]
        if a.type == "HOLD_DEPARTURE":
            return [(f"hold {t} {p['minutes']} min", lambda: w.add_hold(t, int(p["minutes"])),
                     lambda _: w.add_hold(t, -int(p["minutes"])))]
        if a.type == "RETIME_DEPARTURE":
            return [(f"retime {t} +{p['minutes']} min", lambda: w.add_hold(t, int(p["minutes"])),
                     lambda _: w.add_hold(t, -int(p["minutes"])))]
        if a.type == "CANCEL_FLIGHT":
            return [(f"cancel {t}", lambda: w.cancel(t), lambda done: w.uncancel(done))]
        if a.type == "SWAP_AIRCRAFT":
            return [(f"swap {t} onto {p['to_ac']}", lambda: w.swap_aircraft(t, p["to_ac"]),
                     lambda undo: w.unswap_aircraft(undo))]
        if a.type == "SWAP_CREW":
            return [(f"assign {p['crew_in']} to {t} in place of {p['crew_out']}",
                     lambda: w.swap_crew(t, p["crew_out"], p["crew_in"]), lambda undo: w.unswap_crew(undo))]
        if a.type == "REASSIGN_STAND":
            visit = p.get("visit", t)
            return [(f"move {visit} to stand {p['to']}", lambda: w.set_stand(visit, p["to"]),
                     lambda old: w.set_stand(visit, old))]
        if a.type == "HOT_TRANSFER_BAGS":
            runs = int(p.get("runs", 1))
            return [(f"hot-transfer bags {p['inb']} -> {p['out']}", lambda: w.hot_transfer(p["inb"], p["out"], True, runs),
                     lambda _: w.hot_transfer(p["inb"], p["out"], False, runs))]
        if a.type == "PRIORITY_TAG_BAGS":
            return [(f"priority-tag bags {p['inb']} -> {p['out']}", lambda: w.tag_bags(p["inb"], p["out"], True),
                     lambda _: w.tag_bags(p["inb"], p["out"], False))]
        if a.type == "REROUTE_BAGS":
            return [(f"re-route bags of {p['pnr']} to {p['to']}", lambda: w.reroute_bags(p["pnr"], p["to"]),
                     lambda old: w.reroute_bags(p["pnr"], old))]
        if a.type == "REBOOK_GROUP":
            cab, n = p.get("to_cabin", p.get("cabin", "Y")), int(p["size"])
            out: list[Step] = [(f"reserve seat {i + 1}/{n} on {p['to']} for {p['pnr']}",
                                lambda: w.reserve(p["to"], cab, 1), lambda _: w.release(p["to"], cab, 1))
                               for i in range(n)]
            out.append((f"move booking {p['pnr']} to {p['to']}", lambda: self._switch(p["pnr"], p["to"], cab),
                        lambda undo: self._unswitch(p["pnr"], undo)))
            return out
        if a.type == "MASS_REACCOMMODATION":
            return [(f"release the approved re-accommodation plan for {t}", lambda: None, lambda _: None)]
        if a.type == "MEAL_VOUCHER":
            return [(f"issue meal vouchers for {t}", lambda: self._vouchers(p), lambda ids: [w.void_voucher(i) for i in ids])]
        if a.type == "BOOK_HOTEL":
            return [(f"book {rooms} room(s) for {pnr}", (lambda pnr=pnr, rooms=rooms: self._hotel(pnr, rooms)),
                     (lambda done, pnr=pnr: self.hub["hotel"].cancel(pnr) if done else None))
                    for pnr, rooms in sorted(p["rooms_by_pnr"].items())]
        if a.type == "SEND_MESSAGE":
            return [(f"send passenger messages for {t}", lambda: self._messages(case_id, p),
                     lambda ids: [self.store.set("messages", "id", i, status="RETRACTED") for i in ids])]
        if a.type == "STAFF_ALERT":
            return [(f"alert {p.get('to', 'staff')} about {t}", lambda: self.store.add(
                "messages", t=w.now, case_id=case_id, pax="staff", lang="en", template="staff_alert", status="SENT",
                body=f"{t}: expected off-block {C.hm(p.get('etd'))} ({p.get('delay_min')} min late)."),
                lambda i: self.store.set("messages", "id", i, status="RETRACTED"))]
        if a.type == "ESCALATE":
            def notify():
                ids = [self.approvals.notice(case_id, p["to_role"], a.why, a.action_id)]
                if p.get("also"):
                    ids.append(self.approvals.notice(case_id, p["also"], a.why, a.action_id))
                return ids
            return [(f"escalate to {p['to_role']}", notify,
                     lambda ids: [self.store.set("approvals", "approval_id", i, status="WITHDRAWN") for i in ids])]
        raise WriteRejected(f"no write handler for {a.type}")          # e.g. nothing can release an aircraft

    def _switch(self, pnr: str, to: str, cabin: str) -> dict:
        undo = self.world.switch_booking(pnr, to)
        undo["cabin"] = self.world.pnrs[pnr]["cabin"]
        self.world.pnrs[pnr]["cabin"] = cabin
        return undo

    def _unswitch(self, pnr: str, undo: dict) -> None:
        self.world.pnrs[pnr]["cabin"] = undo["cabin"]
        self.world.unswitch_booking(pnr, undo)

    def _vouchers(self, p: dict) -> list[str]:
        done = []
        for pnr, size in sorted(p["pnrs"].items()):
            if self.world.pnrs[pnr]["status"] == "REBOOKED" and pnr not in self.world.vouchers:
                self.world.issue_voucher(pnr, int(p["amount_per_pax"]) * int(size))
                done.append(pnr)
        return done

    def _hotel(self, pnr: str, rooms: int) -> bool:
        g = self.world.pnrs[pnr]
        if not (g["status"] == "REBOOKED" and g["overnight"]) or pnr in self.world.hotel["bookings"]:
            return False                                             # not travelling tomorrow after all / already booked
        self.hub["hotel"].book(pnr, int(rooms))                      # partner call: timeout, retry, circuit breaker
        return True

    def _messages(self, case_id: str, p: dict) -> list[int]:
        ids = []
        for pnr, m in sorted(p["pnrs"].items()):
            if self.world.pnrs[pnr]["status"] == "REBOOKED" and self.world.pnrs[pnr]["cur"] == m["to"]:
                ids.append(self.store.add("messages", t=self.world.now, case_id=case_id, pax=pnr, lang=m["lang"],
                                          template=m["template"], status="SENT",
                                          body=render(m["template"], m["lang"], to=m["to"], etd=C.hm(m["etd"]))))
        return ids

    # ------------------------------------------------------------------ the saga
    def ctx(self, case_id: str) -> GuardCtx:
        def executed(action_id: str) -> bool:
            if action_id in self._inflight:
                return True
            row = self.store.one("SELECT status FROM actions WHERE action_id=?", action_id)
            return bool(row and row["status"] == "EXECUTED")
        return GuardCtx(self.hub, approved=lambda a: self.approvals.for_action(case_id, a), executed=executed)

    def run_bundle(self, case_id: str, actions: list[Action]) -> dict:
        """Run the actions of one bundle as one saga. Returns ok / what failed / what was undone."""
        bundle = actions[0].bundle or actions[0].action_id
        ctx, done, seq = self.ctx(case_id), [], 0
        rules: dict[str, list] = {}
        self._inflight = set()

        def record(a: Action, step: str, status: str, detail: str = "") -> None:
            nonlocal seq
            seq += 1
            self.store.add("saga_steps", case_id=case_id, bundle=bundle, action_id=a.action_id, step=step, seq=seq,
                           status=status, t=self.world.now, detail=detail)

        failure = None
        for a in actions:
            checks = check_action(a, [a], ctx, stage="execute")      # live re-check right before writing
            rules[a.action_id] = [c.model_dump() for c in checks]
            bad = [c for c in checks if not c.passed]
            if bad:
                kind = "data" if all(c.rule == "G8" for c in bad) else "guard"
                failure = {"action": a, "kind": kind, "error": f"{bad[0].rule}: {bad[0].detail}", "rule": bad[0].rule}
                record(a, "re-check guard rules on live data", "FAILED", failure["error"])
                break
            try:
                for name, do, undo in self.steps(case_id, a):
                    try:
                        info = do()
                    except FeedDown as e:
                        failure = {"action": a, "kind": "partner", "error": str(e), "rule": ""}
                        record(a, name, "FAILED", str(e))
                        break
                    except WriteRejected as e:
                        failure = {"action": a, "kind": "write", "error": str(e), "rule": ""}
                        record(a, name, "FAILED", str(e))
                        break
                    self.writes += 1
                    done.append((a, name, undo, info))
                    record(a, name, "DONE")
                    self.world.recompute()
            except WriteRejected as e:
                failure = {"action": a, "kind": "write", "error": str(e), "rule": ""}
                record(a, "find write handler", "FAILED", str(e))
            if failure:
                break
            self._inflight.add(a.action_id)
        undone = []
        if failure:
            for a, name, undo, info in reversed(done):               # compensate in reverse order
                undo(info)
                record(a, "UNDO " + name, "UNDONE")
                if a.action_id not in undone:
                    undone.append(a.action_id)
            self.world.recompute()
        self._inflight = set()
        return {"ok": failure is None, "bundle": bundle, "rules": rules,
                "executed": [] if failure else [a.action_id for a in actions], "undone": undone,
                "failed_action": failure["action"].action_id if failure else "", "kind": failure["kind"] if failure else "",
                "error": failure["error"] if failure else "", "rule": failure["rule"] if failure else "",
                "steps_done": len(done)}
