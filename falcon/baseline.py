"""Baseline: modelled MANUAL operations on the same replayed day (ARCHITECTURE.md section 9).

The same world, the same seed, the same scripted events and the same physical rules as the system run
(a flight still cannot leave with an illegal crew or an unsigned aircraft). What differs is the policy:

  * no proactive holds, no turnaround compression, no hot transfers (bags travel only if the bag MCT is met)
  * every decision (aircraft swap, stand change, crew replacement) takes MANUAL_DECISION_MIN after the
    problem is noticed; crew problems are noticed at report time, not when the delay is first known
  * passengers are rebooked first-come-first-served at a transfer desk: MANUAL_AGENTS agents at
    MANUAL_REBOOK_PAX_PER_AGENT_HOUR each, the earliest flight with seats for the whole group. The agents
    follow the same hard rules (PRM assistance, unaccompanied minors, seats) but apply no priority order.

It is deliberately a competent manual operation, not a straw man: it uses the same swap threshold and the
same spare aircraft as the system.
"""
from __future__ import annotations

from . import config as C
from .models import WriteRejected, signoff_valid
from .replay import OpsDay
from .solvers import crew_legality
from .solvers.propagation import knock_on


class Baseline:
    def __init__(self, seed: int = C.SEED):
        self.world = OpsDay(seed)
        self.first_seen: dict[tuple, int] = {}
        self.queue: list[str] = []
        self.queued: set[str] = set()
        self.credit = 0.0
        self.counts = {"decisions": 0, "swaps": 0, "crew_swaps": 0, "stand_moves": 0, "rebooked_pax": 0,
                       "queue_max_pax": 0, "unresolved_groups": 0}
        self.log: list[tuple] = []

    # ------------------------------------------------------------------ decisions (each MANUAL_DECISION_MIN late)
    def _due(self, key: tuple) -> bool:
        return self.world.now >= self.first_seen.setdefault(key, self.world.now) + C.MANUAL_DECISION_MIN

    def _swaps(self) -> None:
        w, now = self.world, self.world.now
        for f in w.order:
            x = w.legs[f]
            if (x["kind"] != "ARR" or x["ata"] is not None or x["cancelled"]
                    or x["eta"] - x["sta"] < C.SWAP_MIN_INBOUND_DELAY or ("done", f) in self.first_seen):
                continue
            n = w._next(f)
            if not n or w.legs[n]["atd"] is not None or not self._due(("swap", f)):
                continue
            spares = sorted((s for s in w.read("maintenance", "spares")[0] if s["type"] == x["type"]
                             and s["reg"] != x["ac"] and signoff_valid(s, now)), key=lambda s: (not s["spare"], s["reg"]))
            if not spares:
                continue
            ready = now + C.SWAP_PREP_MIN
            if set(w.legs[n]["crew"]) & set(x["crew"]):
                ready = max(ready, x["eta"] + C.CREW_CONNECT_MIN)
            saving = -knock_on(w.legs, w.order, {n: {"prev": None, "not_before": ready}})["delta_min"]
            self.first_seen[("done", f)] = now
            if saving >= C.SWAP_MIN_SAVING_MIN:
                w.swap_aircraft(n, spares[0]["reg"])
                w.recompute()
                self.counts["swaps"] += 1
                self.counts["decisions"] += 1
                self.log.append((now, "SWAP_AIRCRAFT", n, spares[0]["reg"]))

    def _crew(self) -> None:
        w, now = self.world, self.world.now
        for f in w.order:
            leg = w.legs[f]
            if leg["kind"] != "DEP" or leg["atd"] is not None or leg["cancelled"] or leg["std"] - C.CREW_REPORT_MIN > now:
                continue                                    # crew problems are found at report time
            for cid in list(leg["crew"]):
                st = crew_legality.check(w.crew[cid], w.crew_times(cid, f), now)
                if st["legal"] or not self._due(("crew", f, cid)):
                    continue
                opts = crew_legality.legal_options(st["role"], w.crew_times(cid, f), w.read("crew", "standby")[0], now)
                if opts["options"]:
                    w.swap_crew(f, cid, opts["options"][0]["crew"])
                    w.recompute()
                    self.counts["crew_swaps"] += 1
                    self.counts["decisions"] += 1
                    self.log.append((now, "SWAP_CREW", f, opts["options"][0]["crew"]))

    def _stands(self) -> None:
        w = self.world
        conflicts = w.stand_conflicts(60)
        if not conflicts:
            return
        visits = w.visits()
        for c in conflicts:
            if not self._due(("stand", c["stand"], c["a"], c["b"])):
                continue
            v = next((v for v in visits if v["id"] in (c["b"], c["a"]) and not v["on_stand"]), None)
            if not v:
                continue
            for sid, s in w.stands.items():
                if v["type"] in s["types"] and all(o["stand"] != sid or o["end"] <= v["start"] or o["start"] >= v["end"]
                                                   for o in visits if o["id"] != v["id"]):
                    w.set_stand(v["id"], sid)
                    v["stand"] = sid
                    self.counts["stand_moves"] += 1
                    self.counts["decisions"] += 1
                    break

    # ------------------------------------------------------------------ the transfer desk (first come, first served)
    def _join_queue(self) -> None:
        w, now = self.world, self.world.now
        new = [p for p in sorted(w.misconnected) if p not in self.queued]
        for f in w.order:
            leg = w.legs[f]
            if leg["kind"] == "DEP" and leg["cancelled"]:
                new += [p for p in w.by_orig[f] if p not in self.queued and w.pnrs[p]["status"] == "CANCELLED"
                        and now >= w.pnrs[p]["risk_since"] + C.MANUAL_DECISION_MIN]
            elif leg["kind"] == "ARR" and leg["ata"] == now:       # arrived too late for the onward flight
                for p in w.by_in.get(f, []):
                    g = w.pnrs[p]
                    y = w.legs[g["cur"]]
                    if p not in self.queued and g["status"] == "OK" and y["atd"] is None \
                            and now + C.MCT_PAX_MIN > y["etd"]:
                        new.append(p)
        for p in new:
            if p not in self.queued:
                self.queued.add(p)
                self.queue.append(p)

    def _rebook(self, pid: str) -> bool:
        w, now = self.world, self.world.now
        g = w.pnrs[pid]
        dest = w.legs[g["out"]]["dest"]
        for f in sorted(w.to_dest[dest], key=lambda f: (w.legs[f]["etd"], f)):
            leg = w.legs[f]
            if f == g["cur"] or leg["cancelled"] or leg["atd"] is not None or leg["etd"] < now + C.MCT_PAX_MIN:
                continue
            if "WCHR" in g["ssr"] and not leg["prm_assist"]:
                continue
            if "UMNR" in g["ssr"] and (leg["kind"] == "ND" or not leg["umnr_escort"]):
                continue
            for cabin in ([g["cabin"], "Y"] if g["cabin"] == "J" else ["Y"]):
                if leg["free_" + cabin.lower()] >= g["size"]:
                    try:
                        w.reserve(f, cabin, g["size"])
                    except WriteRejected:
                        continue
                    w.switch_booking(pid, f)
                    g["cabin"] = cabin
                    w.reroute_bags(pid, f)
                    self.counts["rebooked_pax"] += g["size"]
                    return True
        return False

    def _desk(self) -> None:
        w = self.world
        self._join_queue()
        waiting = sum(w.pnrs[p]["size"] for p in self.queue)
        self.counts["queue_max_pax"] = max(self.counts["queue_max_pax"], waiting)
        if not self.queue:
            self.credit = 0.0
            return
        self.credit += C.MANUAL_AGENTS * C.MANUAL_REBOOK_PAX_PER_AGENT_HOUR / 60.0
        while self.queue and self.credit >= w.pnrs[self.queue[0]]["size"]:
            pid = self.queue.pop(0)
            self.credit -= w.pnrs[pid]["size"]
            if w.pnrs[pid]["status"] not in ("MISCONNECTED", "CANCELLED", "OK"):
                continue
            if not self._rebook(pid):
                self.counts["unresolved_groups"] += 1          # e.g. an unaccompanied minor with no same-day escort

    # ------------------------------------------------------------------ run
    def tick(self) -> int:
        now = self.world.step()
        self._swaps()
        self._crew()
        self._stands()
        self._desk()
        return now

    def run_until(self, minute: int) -> int:
        while self.world.now < minute:
            self.tick()
        return self.world.now

    def results(self) -> dict:
        m = self.world.metrics()
        staff_h = self.counts["rebooked_pax"] / C.MANUAL_REBOOK_PAX_PER_AGENT_HOUR
        return {**m, "agent_hours": round(staff_h, 1),
                "controller_hours": round(self.counts["decisions"] * C.MANUAL_DECISION_MIN / 60, 1),
                "decisions": self.counts["decisions"], "counts": dict(self.counts)}


def run(seed: int = C.SEED, until: int = C.DAY_END) -> dict:
    b = Baseline(seed)
    b.run_until(until)
    return b.results()
