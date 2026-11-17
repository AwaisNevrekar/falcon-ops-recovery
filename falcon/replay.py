"""Simulated clock + the replayed operations day.

`OpsDay` is the mock "systems of record": schedule, movements, crew, maintenance, passengers, bags,
seats, stands and the hotel partner. Feed adapters READ it. Only the write gateway (saga.py) and the
manual baseline policy WRITE it. It also plays the scripted disruption events and injects feed faults
(stale, missing, conflicting, down), so the same seeded day can be replayed again and again.
"""
from __future__ import annotations

import copy
import importlib.util
from collections import defaultdict
from pathlib import Path

from . import config as C
from .models import FeedDown, WriteRejected, signoff_valid
from .solvers import crew_legality
from .solvers.propagation import compute, reactionary

_spec = importlib.util.spec_from_file_location(
    "falcon_generate_day", Path(__file__).resolve().parent.parent / "data" / "generate_day.py")
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)
generate = _gen.generate

# name, start offset, end offset (on a 45-minute narrow-body turnaround), team
TASKS = [("deboarding", 0, 8, "gate"), ("bag_unload", 2, 14, "baggage"), ("cleaning", 8, 20, "cleaning"),
         ("catering", 8, 20, "catering"), ("fuelling", 8, 22, "fuelling"), ("bag_load", 18, 36, "baggage"),
         ("boarding", 22, 40, "gate"), ("pushback_prep", 40, 45, "ramp")]
_DYN = {"pdelay": 0, "atfm": 0, "hold": 0, "turn_cut": 0, "not_before": 0, "block_delay": 0, "stand_wait": 0,
        "cancelled": False, "atd": None, "ata": None, "blocked": None}


class SimClock:
    def __init__(self, start: int = C.REPLAY_START):
        self.now = start

    def advance(self, minutes: int = 1) -> int:
        self.now += minutes
        return self.now


class OpsDay:
    def __init__(self, seed: int = C.SEED, start: int = C.REPLAY_START, with_events: bool = True):
        d = generate(seed)
        self.seed, self.dests, self.stands, self.story = seed, d["dests"], dict(d["stands"]), d["story"]
        self.legs: dict[str, dict] = {}
        for f, leg in d["legs"].items():
            self.legs[f] = {**leg, **_DYN, "crew": list(leg["crew"]), "crew_prev": [], "measures": [],
                            "etd": leg["std"], "eta": leg["sta"]}
        for code, dd in self.dests.items():        # next-day capacity used for overnight re-accommodation
            std = 1440 + 420
            self.legs["ND-" + code] = {
                **_DYN, "fid": "ND-" + code, "ac": "", "origin": C.HUB, "dest": code, "std": std,
                "sta": std + dd["block"], "block": dd["block"], "type": "WB", "kind": "ND", "seats_j": 40,
                "seats_y": 400, "free_j": 40, "free_y": 400, "crew": [], "crew_prev": [], "measures": [], "stand": None,
                "prev": None, "story": "", "prm_assist": True, "umnr_escort": False, "local_pax": 0, "turn": 0,
                "floor": 0, "etd": std, "eta": std + dd["block"]}
        self.rot = {a: list(r) for a, r in d["rot"].items()}
        self.crew = {c: {**v, "legs": list(v["legs"])} for c, v in d["crew"].items()}
        self.aircraft = copy.deepcopy(d["aircraft"])
        self.pnrs = {i: {**p, "ssr": list(p["ssr"]), "cur": p["out"], "status": "OK", "risk_since": None,
                         "rebooked_at": None, "missed": False, "bags_missed": 0, "hot": None, "rush": False,
                         "bags_to": p["out"], "overnight": False, "hist": []} for i, p in d["pnrs"].items()}
        self.events = list(d["events"]) if with_events else []
        self.order = sorted((f for f, x in self.legs.items() if x["kind"] != "ND"),
                            key=lambda f: (self.legs[f]["std"], f))
        self.by_cur: dict[str, set] = defaultdict(set)
        self.by_in: dict[str, list] = defaultdict(list)
        for i, p in self.pnrs.items():
            self.by_cur[p["cur"]].add(i)
            if p["inb"]:
                self.by_in[p["inb"]].append(i)
        self.to_dest: dict[str, list] = defaultdict(list)
        for f in self.order:
            if self.legs[f]["kind"] == "DEP":
                self.to_dest[self.legs[f]["dest"]].append(f)
        for code in self.dests:
            self.to_dest[code].append("ND-" + code)
        self.faults: list[dict] = []
        self.notes: dict[str, list] = defaultdict(list)
        self.applied: set[str] = set()
        self.event_log: list[tuple] = []
        self.weather = {"condition": "CLEAR", "arrival_rate_cut": 0.0, "until": None}
        self.seat_race: dict | None = None
        self.on_stand: dict[str, str] = {}
        self.tows: list[tuple] = []
        self.resources: dict[tuple, int] = defaultdict(int)     # (kind, 30-min window) -> used
        self.hotel = {"rooms": C.HOTEL_ROOMS, "bookings": {}}
        self.vouchers: dict[str, int] = {}
        self.reserved: dict[str, str] = {}                      # "crew:ID" / "aircraft:REG" -> holder
        for f in self.order:
            self._set_crew_prev(f)
        self._ff = True                 # flights before the replay start flew exactly on schedule
        self.now = start - 1
        self.step(start)
        self._ff = False
        self._rebuild_on_stand()

    # ------------------------------------------------------------------ helpers
    def _next(self, fid: str) -> str | None:
        r = self.rot.get(self.legs[fid]["ac"], [])
        i = r.index(fid) if fid in r else -1
        for nf in r[i + 1:] if i >= 0 else []:
            if not self.legs[nf]["cancelled"]:
                return nf
        return None

    def _set_crew_prev(self, fid: str) -> None:
        leg, out = self.legs[fid], set()
        for cid in leg["crew"]:
            cl = self.crew[cid]["legs"]
            i = cl.index(fid) if fid in cl else 0
            if i > 0 and self.legs[cl[i - 1]]["ac"] != leg["ac"]:
                out.add(cl[i - 1])
        leg["crew_prev"] = sorted(out)

    def crew_times(self, cid: str, fid: str) -> list[tuple[str, int, int]]:
        """Remaining legs of a crew member from `fid` onward, with current estimates."""
        cl = self.crew[cid]["legs"]
        rest = cl[cl.index(fid):] if fid in cl else [fid]
        return [(f, self.legs[f]["etd"], self.legs[f]["eta"]) for f in rest if not self.legs[f]["cancelled"]]

    def blocked(self, fid: str) -> str | None:
        """Why a hub departure cannot physically leave right now (the day itself never breaks a safety rule)."""
        leg = self.legs[fid]
        if leg["origin"] != C.HUB or leg["kind"] != "DEP":
            return None
        if not signoff_valid(self.aircraft.get(leg["ac"]), self.now):
            return "NO_SIGNOFF"
        for cid in leg["crew"]:
            if not crew_legality.check(self.crew[cid], self.crew_times(cid, fid), self.now)["legal"]:
                return "CREW_LIMIT"
        return None

    # ------------------------------------------------------------------ time
    def recompute(self) -> None:
        for f, (etd, eta) in compute(self.legs, self.order).items():
            leg = self.legs[f]
            leg["etd"], leg["eta"] = etd, eta

    def step(self, now: int | None = None) -> int:
        """Advance the day to `now` (default: one minute): play due events, re-estimate, freeze actual times."""
        self.now = self.now + 1 if now is None else now
        for e in self.events:
            if e["t"] <= self.now and e["id"] not in self.applied:
                self.apply_event(e)
        self.faults = [f for f in self.faults if f["until"] > self.now]
        self.recompute()
        self._freeze()
        self.recompute()
        self._update_risk()
        return self.now

    def _freeze(self) -> None:
        now = self.now
        for f in self.order:
            leg = self.legs[f]
            if leg["cancelled"] or leg["ata"] is not None:
                continue
            hub_dep = leg["origin"] == C.HUB
            if leg["atd"] is None:
                if hub_dep and leg["prev"] is None and now >= leg["std"] - 60 and not self._ff:
                    self.on_stand.setdefault(leg["stand"], leg["ac"])
                if leg["etd"] > now:
                    continue
                why = self.blocked(f) if hub_dep else None
                if why:                                   # waits for a legal crew / a signed-off aircraft
                    leg["blocked"], leg["not_before"] = why, now + 1
                    leg["block_delay"] += 1
                    continue
                leg["atd"], leg["blocked"] = leg["etd"], None
                if hub_dep:
                    if self.on_stand.get(leg["stand"]) == leg["ac"]:
                        del self.on_stand[leg["stand"]]
                    self._on_departure(leg)
            if leg["eta"] <= now:
                if leg["dest"] == C.HUB and not self._ff:
                    occ = self.on_stand.get(leg["stand"])
                    if occ and occ != leg["ac"]:          # stand still occupied: wait, then park remote
                        if leg["stand_wait"] < C.STAND_WAIT_MAX_MIN:
                            leg["stand_wait"] += 1
                            leg["eta"] += 1
                            continue
                        self._park_remote(leg)
                    self.on_stand[leg["stand"]] = leg["ac"]
                    if self._next(f) is None:
                        self.tows.append((now + 60, leg["stand"], leg["ac"]))
                leg["ata"] = leg["eta"]
        for t, stand, ac in self.tows:
            if t <= now and self.on_stand.get(stand) == ac:
                del self.on_stand[stand]
        self.tows = [x for x in self.tows if x[0] > now]

    def _rebuild_on_stand(self) -> None:
        self.on_stand = {}
        for ac, r in self.rot.items():
            for i, f in enumerate(r):
                leg = self.legs[f]
                if leg["kind"] != "DEP" or leg["cancelled"] or leg["atd"] is not None:
                    continue
                p = self.legs[r[i - 1]] if i else None
                if (p is None and self.now >= leg["std"] - 60) or (p is not None and p["ata"] is not None):
                    self.on_stand[leg["stand"]] = ac
                break

    def _park_remote(self, leg: dict) -> None:
        for sid, s in self.stands.items():
            if not s["contact"] and leg["type"] in s["types"] and sid not in self.on_stand:
                self.set_stand(leg["fid"], sid)
                return

    def _on_departure(self, leg: dict) -> None:
        """A hub departure has left: who (and whose bags) made it?"""
        fid = leg["fid"]
        for pid in list(self.by_cur.get(fid, ())):
            p = self.pnrs[pid]
            if p["status"] not in ("OK", "REBOOKED"):
                continue
            arr = 0
            if p["inb"]:
                x = self.legs[p["inb"]]
                arr = None if x["cancelled"] else (x["ata"] if x["ata"] is not None else x["eta"])
                if arr is None or arr + C.MCT_PAX_MIN > leg["atd"]:
                    p["status"], p["missed"] = "MISCONNECTED", True
                    if p["risk_since"] is None:
                        p["risk_since"] = self.now
                    continue
            bag_mct = C.HOT_TRANSFER_MIN if p["hot"] == fid else C.MCT_BAG_MIN
            if p["bags"] and (p["bags_to"] != fid or (p["inb"] and arr + bag_mct > leg["atd"])):
                p["bags_missed"] = p["bags"]
            p["status"] = "FLOWN"

    def _update_risk(self) -> None:
        """Stamp the minute a connection first became impossible (used to measure re-accommodation time)."""
        for f, ids in self.by_in.items():
            x = self.legs[f]
            if x["eta"] - x["sta"] < 1 and not x["cancelled"]:
                continue
            for pid in ids:
                p = self.pnrs[pid]
                if p["status"] != "OK":
                    continue
                y = self.legs[p["cur"]]
                if y["atd"] is not None:
                    continue
                risk = x["cancelled"] or y["cancelled"] or x["eta"] + C.MCT_PAX_MIN > y["etd"]
                if risk and p["risk_since"] is None:
                    p["risk_since"] = self.now
                elif not risk and p["risk_since"] is not None:
                    p["risk_since"] = None

    # ------------------------------------------------------------------ scripted events and faults
    def apply_event(self, e: dict) -> None:
        self.applied.add(e["id"])
        self.event_log.append((self.now, e["id"], e["title"], e.get("shows", "")))
        kind = e["kind"]
        if e.get("stale"):                 # capture the last record the feed delivered before it froze
            s = e["stale"]
            self.faults.append({"feed": s["feed"], "key": e["flight"], "kind": "stale", "since": s["since"],
                                "until": s["until"], "frozen": getattr(self, "_rd_" + s["feed"])(e["flight"])})
        if e.get("tech_fault"):
            ac = self.aircraft[e["tech_fault"]["aircraft"]]
            ac["signoff"] = None
            ac["open_items"].append(e["tech_fault"]["item"])
        if kind == "weather":
            self.weather = {"condition": "SANDSTORM", "arrival_rate_cut": e["cut"], "until": self.now + e["duration"]}
            self._meter_arrivals(e["cut"], e["duration"])
        elif kind == "delay":
            self._delay(e)
        elif kind == "cancel":
            self.cancel(e["flight"])
        elif kind == "signoff":
            self.record_signoff(e["aircraft"], e["by"])
        elif kind == "stand_double":
            self.set_stand(e["flight"], e["stand"])
        elif kind == "eta_conflict":
            self._delay({"flight": e["flight"], "minutes": e["atc"]})
            self.faults.append({"feed": "movements", "key": e["flight"], "kind": "conflict",
                                "offset": e["airline"] - e["atc"], "since": self.now, "until": e["until"]})
        elif kind == "partner_down":
            self.faults.append({"feed": e["feed"], "key": None, "kind": "down", "since": self.now, "until": e["until"]})
        if e.get("note"):
            self.notes[self._next(e["flight"]) or e["flight"]].append(e["note"])
        for f, n in (e.get("seats") or {}).items():
            self.legs[f]["free_y"], self.legs[f]["free_j"] = n, 0
        if e.get("seat_race"):
            self.seat_race = {**e["seat_race"], "count": 0}

    def _delay(self, e: dict) -> None:
        x = self.legs[e["flight"]]
        self.recompute()
        if "need_hold" in e:               # late enough that the outbound must hold `need_hold` min
            m = self.legs[e["outbound"]]["etd"] + e["need_hold"] - C.MCT_PAX_MIN - x["eta"]
        elif "dep_delay" in e:             # late enough that the next departure leaves `dep_delay` late
            y = self.legs[e["outbound"]]
            m = y["std"] + e["dep_delay"] - y["turn"] - x["eta"]
        else:
            m = e["minutes"]
        if m > 0:
            x["pdelay" if x["atd"] is None else "atfm"] += m     # on the ground: late departure; airborne: late arrival

    def _meter_arrivals(self, cut: float, duration: int) -> None:
        """Reduced arrival rate: queue the arrivals and give each one its slot delay."""
        t0, t1 = self.now, self.now + duration
        normal = 60 / C.ARRIVAL_RATE_PER_H
        slow = normal / (1 - cut)
        queue = sorted((x for x in self.legs.values() if x["kind"] == "ARR" and x["ata"] is None
                        and not x["cancelled"] and not x["story"] and x["eta"] >= t0), key=lambda x: (x["eta"], x["fid"]))
        slot = float(t0)
        for x in queue:
            if x["eta"] > t1 and slot <= x["eta"]:
                break                                       # queue has cleared after the storm
            slot = max(x["eta"], slot + (slow if slot < t1 else normal / C.STORM_RECOVERY_FACTOR))
            if round(slot - x["eta"]) > 0:
                x["atfm"] += int(round(slot - x["eta"]))

    def add_fault(self, feed: str, kind: str, until: int, key: str | None = None, **extra) -> None:
        """Outage switch for the dashboard and tests (kind: stale | missing | down | slow)."""
        frozen = None
        if kind == "stale":
            frozen = getattr(self, "_rd_" + feed)(key) if key else {"alive": True}
        self.faults.append({"feed": feed, "key": key, "kind": kind, "since": extra.pop("since", self.now),
                            "until": until, "frozen": frozen, **extra})

    def cancel(self, fid: str) -> list[str]:
        """Cancel a hub departure and its return leg (the aircraft stays at the hub)."""
        done = []
        for f in (fid, self._next(fid) if self.legs[fid]["kind"] == "DEP" else None):
            if f and self.legs[f]["atd"] is None and not self.legs[f]["cancelled"]:
                self.legs[f]["cancelled"] = True
                done.append(f)
        for pid in self.by_cur.get(fid, ()):
            p = self.pnrs[pid]
            if p["status"] in ("OK", "REBOOKED"):
                p["status"], p["risk_since"] = "CANCELLED", self.now
        return done

    def record_signoff(self, reg: str, by: str) -> None:
        """A HUMAN in Maintenance Control records a release. No agent, solver or saga step can call this."""
        if not by or by.lower().startswith(("agent", "system", "auto", "llm")):
            raise WriteRejected("a maintenance release can only be recorded by Maintenance Control staff")
        self.aircraft[reg]["open_items"] = []
        self.aircraft[reg]["signoff"] = {"by": by, "at": self.now, "valid_until": self.now + 1440}

    # ------------------------------------------------------------------ feed reads (value, updated_at)
    def _fault(self, feed: str, key: str | None) -> dict | None:
        for f in self.faults:
            if f["feed"] == feed and f["key"] in (None, key) and f["since"] <= self.now < f["until"]:
                return f
        return None

    def read(self, feed: str, key: str = "*"):
        f = self._fault(feed, None if key == "__hb__" else key)
        if key == "__hb__" and f and f["key"] is not None:
            f = None
        if f:
            if f["kind"] == "down":
                raise FeedDown(f"{feed} is not answering")
            if f["kind"] == "slow":
                raise TimeoutError(f"{feed} timed out")
            if f["kind"] == "missing":
                return None, self.now
            if f["kind"] == "stale":
                return f["frozen"], f["since"]
        if key == "__hb__":
            return {"alive": True}, self.now
        return getattr(self, "_rd_" + feed)(key), self.now

    def _mov(self, f: str) -> dict:
        x = self.legs[f]
        status = ("CANCELLED" if x["cancelled"] else "ARRIVED" if x["ata"] is not None
                  else "AIRBORNE" if x["atd"] is not None else "SCHEDULED")
        return {"fid": f, "kind": x["kind"], "ac": x["ac"], "origin": x["origin"], "dest": x["dest"], "std": x["std"],
                "sta": x["sta"], "etd": x["etd"], "eta": x["eta"], "atd": x["atd"], "ata": x["ata"], "status": status,
                "dep_delay": x["etd"] - x["std"], "arr_delay": x["eta"] - x["sta"], "blocked": x["blocked"]}

    def _rd_movements(self, key: str):
        if key != "*":
            rec = self._mov(key)
            c = self._fault("movements", key)
            if c and c["kind"] == "conflict":          # the airline's own estimate lags behind ATC
                rec["eta"] += c["offset"]
                rec["arr_delay"] += c["offset"]
            return rec
        out = []
        for f in self.order:
            if self.legs[f]["ata"] is not None:
                continue
            rec, upd = self.read("movements", f)       # honours per-flight faults
            if rec and (rec["status"] == "CANCELLED" or rec["dep_delay"] >= 5 or rec["arr_delay"] >= 5 or upd < self.now):
                out.append({**rec, "upd": upd})
        return out

    def _rd_atc(self, key: str):
        if key != "*":
            x = self.legs[key]
            return {"fid": key, "eta": x["eta"], "sta": x["sta"], "slot_delay": x["atfm"]}
        return [{"fid": f, "eta": x["eta"], "sta": x["sta"], "arr_delay": x["eta"] - x["sta"], "upd": self.now}
                for f in self.order for x in [self.legs[f]]
                if x["kind"] == "ARR" and x["ata"] is None and not x["cancelled"] and x["eta"] - x["sta"] >= 5]

    def _rd_schedule(self, key: str):
        if key == "legs":                  # live reference for the propagation calculator (read-only by convention)
            return {"legs": self.legs, "order": self.order, "rot": self.rot}
        if key.startswith("to:"):
            return [f for f in self.to_dest.get(key[3:], [])
                    if not self.legs[f]["cancelled"] and self.legs[f]["atd"] is None]
        x = self.legs[key]
        return {k: x[k] for k in ("fid", "ac", "origin", "dest", "std", "sta", "block", "type", "kind", "prev", "stand",
                                  "prm_assist", "umnr_escort", "cancelled", "seats_j", "seats_y", "local_pax")} | {
            "crew": list(x["crew"]), "next": self._next(key) if x["kind"] != "ND" else None}

    def _rd_turnaround(self, key: str):
        x = self.legs[key]
        p = self.legs.get(x["prev"]) if x["prev"] else None
        inblock = p["eta"] if p else x["std"] - 60
        turn = max(x["floor"], x["turn"] - x["turn_cut"])
        tasks = []
        for name, a, b, team in TASKS:
            s, e = inblock + round(a * turn / 45), inblock + round(b * turn / 45)
            tasks.append({"task": name, "team": team, "start": s, "end": e,
                          "status": "DONE" if self.now >= e else "STARTED" if self.now >= s else "PLANNED"})
        return {"fid": key, "inbound": x["prev"], "inblock": inblock, "std": x["std"], "predicted_offblock": x["etd"],
                "turn_min": turn, "min_turn": x["turn"], "floor_turn": x["floor"], "turn_cut": x["turn_cut"],
                "measures": list(x["measures"]), "tasks": tasks, "notes": list(self.notes.get(key, [])),
                "resources": {m: v["pool"] - self.resources[(m, self.now // 30)] for m, v in C.GROUND_MEASURES.items()}}

    def visits(self) -> list[dict]:
        """Every aircraft ground visit at the hub that is still to come or in progress."""
        out = []
        for ac, r in self.rot.items():
            seq = [self.legs[f] for f in r if not self.legs[f]["cancelled"]]
            for i, x in enumerate(seq):
                if x["kind"] != "DEP" or x["atd"] is not None:
                    continue
                p = seq[i - 1] if i else None
                start = (p["eta"] if p else max(self.now, x["std"] - 60)) - C.STAND_BUFFER_MIN
                out.append({"id": x["fid"], "arr": p["fid"] if p else None, "dep": x["fid"], "ac": ac, "type": x["type"],
                            "start": start, "end": x["etd"] + C.STAND_BUFFER_MIN, "stand": x["stand"],
                            "on_stand": p is None or p["ata"] is not None,
                            "pax": (x["seats_j"] + x["seats_y"] - x["free_j"] - x["free_y"]),
                            "boarding": self.now >= x["etd"] - 40})
            if seq and seq[-1]["kind"] == "ARR" and seq[-1]["ata"] is None:
                x = seq[-1]
                out.append({"id": x["fid"], "arr": x["fid"], "dep": None, "ac": ac, "type": x["type"],
                            "start": x["eta"] - C.STAND_BUFFER_MIN, "end": x["eta"] + 60, "stand": x["stand"],
                            "on_stand": False, "pax": x["local_pax"], "boarding": False})
        return out

    def stand_conflicts(self, horizon: int = C.STAND_HORIZON_MIN) -> list[dict]:
        by_stand = defaultdict(list)
        for v in self.visits():
            if v["stand"] and v["start"] <= self.now + horizon:
                by_stand[v["stand"]].append(v)
        out = []
        for stand, vs in sorted(by_stand.items()):
            vs.sort(key=lambda v: (v["start"], v["id"]))
            for i, a in enumerate(vs):
                for b in vs[i + 1:]:
                    if b["start"] < a["end"]:
                        out.append({"stand": stand, "a": a["id"], "b": b["id"], "from": b["start"],
                                    "to": min(a["end"], b["end"])})
        return out

    def _rd_stands(self, key: str):
        if key == "visits":
            return self.visits()
        if key == "conflicts":
            return self.stand_conflicts()
        if key == "stands":
            return self.stands
        return {"fid": key, "stand": self.legs[key]["stand"], **{k: self.stands[self.legs[key]["stand"]][k]
                                                                for k in ("contact", "walk", "types")}}

    def _rd_crew(self, key: str):
        if key == "standby":
            return [{**c, "legs": list(c["legs"])} for c in self.crew.values() if c["standby"] and not c["legs"]]
        return [{**self.crew[c], "legs": list(self.crew[c]["legs"]), "times": self.crew_times(c, key)}
                for c in self.legs[key]["crew"]]

    def _ac_view(self, reg: str) -> dict:
        a = self.aircraft[reg]
        free = all(self.legs[f]["ata"] is not None or self.legs[f]["cancelled"] for f in self.rot.get(reg, []))
        return {"reg": reg, "type": a["type"], "spare": a["spare"], "signoff": copy.deepcopy(a["signoff"]),
                "open_items": list(a["open_items"]), "free_at_hub": free and f"aircraft:{reg}" not in self.reserved}

    def _rd_maintenance(self, key: str):
        if key == "spares":
            return [v for v in (self._ac_view(r) for r in sorted(self.aircraft)) if v["free_at_hub"]]
        return self._ac_view(key)

    def _group(self, p: dict) -> dict:
        """What agents may see about a booking: the hashed id only — never a name or booking reference."""
        return {"pnr": p["id"], "size": p["size"], "cabin": p["cabin"], "ssr": list(p["ssr"]), "family": p["family"],
                "onward": p["onward"], "inb": p["inb"], "out": p["cur"], "orig": p["out"], "bags": p["bags"],
                "status": p["status"], "lang": p["lang"], "booked": p["booked"], "loyalty": p["loyalty"],
                "hot": p["hot"], "rush": p["rush"], "bags_to": p["bags_to"], "overnight": p["overnight"]}

    def _rd_pax(self, key: str):
        if key.startswith("out:"):
            ids = sorted(self.by_cur.get(key[4:], ()))
        elif key.startswith("pnr:"):
            ids = [key[4:]]
        else:
            ids = self.by_in.get(key, [])
        return [self._group(self.pnrs[i]) for i in ids]

    def _rd_baggage(self, key: str):
        return [{"pnr": p["id"], "bags": p["bags"], "to": p["cur"], "hot": p["hot"], "rush": p["rush"],
                 "ssr": list(p["ssr"])}
                for p in (self.pnrs[i] for i in self.by_in.get(key, [])) if p["bags"] and p["status"] in ("OK", "REBOOKED")]

    def _rd_inventory(self, key: str):
        x = self.legs[key]
        return {"fid": key, "free_j": x["free_j"], "free_y": x["free_y"]}

    def _rd_hotel(self, key: str):
        return {"rooms": self.hotel["rooms"], "rate": C.HOTEL_RATE_AED}

    def _rd_weather(self, key: str):
        if self.weather["until"] is not None and self.now >= self.weather["until"]:
            self.weather = {"condition": "CLEAR", "arrival_rate_cut": 0.0, "until": None}
        return dict(self.weather)

    # ------------------------------------------------------------------ writes (write gateway / baseline only)
    def _open(self, fid: str) -> dict:
        leg = self.legs[fid]
        if leg["cancelled"] or leg["atd"] is not None:
            raise WriteRejected(f"{fid} has already left or is cancelled")
        return leg

    def add_hold(self, fid: str, minutes: int) -> None:
        leg = self._open(fid) if minutes > 0 else self.legs[fid]
        leg["hold"] = max(0, leg["hold"] + minutes)

    def dispatch(self, fid: str, measure: str, undo: bool = False) -> None:
        leg, m = self.legs[fid], C.GROUND_MEASURES[measure]
        key = (measure, self.now // 30)
        if undo:
            if measure in leg["measures"]:
                leg["measures"].remove(measure)
                leg["turn_cut"] -= m["saves"]
                self.resources[key] = max(0, self.resources[key] - 1)
            return
        self._open(fid)
        if measure in leg["measures"]:
            raise WriteRejected(f"{measure} already dispatched for {fid}")
        if self.resources[key] >= m["pool"]:
            raise WriteRejected(f"no {measure} available in this half hour")
        self.resources[key] += 1
        leg["measures"].append(measure)
        leg["turn_cut"] += m["saves"]

    def swap_aircraft(self, fid: str, new_ac: str) -> dict:
        leg = self._open(fid)
        old = leg["ac"]
        if not signoff_valid(self.aircraft.get(new_ac), self.now):
            raise WriteRejected(f"{new_ac} has no current maintenance sign-off")
        if self.aircraft[new_ac]["type"] != leg["type"]:
            raise WriteRejected(f"{new_ac} is not a {leg['type']}")
        if any(self.legs[f]["ata"] is None and not self.legs[f]["cancelled"] for f in self.rot[new_ac]):
            raise WriteRejected(f"{new_ac} is not free at the hub")
        i = self.rot[old].index(fid)
        moved = self.rot[old][i:]
        undo = {"fid": fid, "old": old, "new": new_ac, "moved": moved, "prev": leg["prev"],
                "not_before": leg["not_before"], "new_rot": list(self.rot[new_ac])}
        self.rot[old], self.rot[new_ac] = self.rot[old][:i], self.rot[new_ac] + moved
        for f in moved:
            self.legs[f]["ac"] = new_ac
        leg["prev"] = self.rot[new_ac][-len(moved) - 1] if len(self.rot[new_ac]) > len(moved) else None
        leg["not_before"] = max(leg["not_before"], self.now + C.SWAP_PREP_MIN)
        for f in moved:
            self._set_crew_prev(f)
        return undo

    def unswap_aircraft(self, u: dict) -> None:
        self.rot[u["new"]] = u["new_rot"]
        self.rot[u["old"]] = self.rot[u["old"]] + u["moved"]
        for f in u["moved"]:
            self.legs[f]["ac"] = u["old"]
        self.legs[u["fid"]]["prev"], self.legs[u["fid"]]["not_before"] = u["prev"], u["not_before"]
        for f in u["moved"]:
            self._set_crew_prev(f)

    def swap_crew(self, fid: str, out_id: str, in_id: str) -> dict:
        leg = self._open(fid)
        c_out, c_in = self.crew[out_id], self.crew[in_id]
        if fid not in c_out["legs"] or c_in["legs"]:
            raise WriteRejected("crew assignment changed since the proposal")
        i = c_out["legs"].index(fid)
        moved = c_out["legs"][i:]
        undo = {"fid": fid, "out": out_id, "in": in_id, "moved": moved, "duty_start": c_in["duty_start"],
                "not_before": leg["not_before"]}
        c_out["legs"], c_in["legs"] = c_out["legs"][:i], list(moved)
        if c_in["duty_start"] is None:
            c_in["duty_start"] = self.now
        for f in moved:
            self.legs[f]["crew"] = [in_id if c == out_id else c for c in self.legs[f]["crew"]]
            self._set_crew_prev(f)
        ready = max(self.now, c_in.get("available_from") or 0) + C.STANDBY_CALLOUT_MIN
        leg["not_before"] = max(leg["not_before"], ready)
        return undo

    def unswap_crew(self, u: dict) -> None:
        c_out, c_in = self.crew[u["out"]], self.crew[u["in"]]
        c_out["legs"], c_in["legs"], c_in["duty_start"] = c_out["legs"] + u["moved"], [], u["duty_start"]
        for f in u["moved"]:
            self.legs[f]["crew"] = [u["out"] if c == u["in"] else c for c in self.legs[f]["crew"]]
            self._set_crew_prev(f)
        self.legs[u["fid"]]["not_before"] = u["not_before"]

    def set_stand(self, fid: str, stand: str) -> str:
        """Move a whole ground visit (the arrival and the departure that follows it). Returns the old stand."""
        leg = self.legs[fid]
        old = leg["stand"]
        pair = [fid]
        if leg["kind"] == "ARR" and self._next(fid):
            pair.append(self._next(fid))
        elif leg["kind"] == "DEP" and leg["prev"] and self.legs[leg["prev"]]["ac"] == leg["ac"]:
            pair.append(leg["prev"])
        for f in pair:
            self.legs[f]["stand"] = stand
        if self.on_stand.get(old) == leg["ac"]:
            del self.on_stand[old]
            self.on_stand[stand] = leg["ac"]
        return old

    def seats_free(self, fid: str, cabin: str) -> int:
        return self.legs[fid]["free_" + cabin.lower()]

    def reserve(self, fid: str, cabin: str, n: int = 1) -> None:
        leg, k = self._open(fid), "free_" + cabin.lower()
        r = self.seat_race
        if r and r["flight"] == fid and r["count"] >= r["after"]:     # someone else buys a seat mid-saga
            leg[k] = max(0, leg[k] - r["take"])
            self.seat_race = None
        if leg[k] < n:
            raise WriteRejected(f"no {cabin} seat left on {fid} (inventory changed)")
        leg[k] -= n
        if self.seat_race and self.seat_race["flight"] == fid:
            self.seat_race["count"] += n

    def release(self, fid: str, cabin: str, n: int = 1) -> None:
        self.legs[fid]["free_" + cabin.lower()] += n

    def switch_booking(self, pid: str, to: str) -> dict:
        """Move a group to flight `to` (seats there must already be reserved). Returns the undo record."""
        p = self.pnrs[pid]
        old = self.legs[p["cur"]]
        refund = old["atd"] is None and not old["cancelled"]
        if refund:
            self.release(old["fid"], p["cabin"], p["size"])
        undo = {k: p[k] for k in ("cur", "status", "rebooked_at", "overnight", "missed")} | {"refund": refund}
        self.by_cur[p["cur"]].discard(pid)
        self.by_cur[to].add(pid)
        if p["inb"] and p["status"] in ("OK", "MISCONNECTED"):
            p["missed"] = True                              # they do not fly the connection they booked
        if p["risk_since"] is None:
            p["risk_since"] = self.now
        p.update(cur=to, status="REBOOKED", rebooked_at=self.now, overnight=self.legs[to]["kind"] == "ND")
        p["hist"].append((self.now, old["fid"], to))
        return undo

    def unswitch_booking(self, pid: str, u: dict) -> None:
        p = self.pnrs[pid]
        self.by_cur[p["cur"]].discard(pid)
        self.by_cur[u["cur"]].add(pid)
        if u["refund"]:
            self.legs[u["cur"]]["free_" + p["cabin"].lower()] -= p["size"]
        p.update({k: u[k] for k in ("cur", "status", "rebooked_at", "overnight", "missed")})
        if p["hist"]:
            p["hist"].pop()

    def hot_transfer(self, inb: str, out: str, on: bool = True) -> int:
        key = ("hot_transfer", self.now // 30)
        if on:
            self._open(out)
            if self.resources[key] >= C.HOT_TRANSFER_RUNS_PER_30MIN:
                raise WriteRejected("no hot-transfer vehicle free in this half hour")
            self.resources[key] += 1
        else:
            self.resources[key] = max(0, self.resources[key] - 1)
        n = 0
        for pid in self.by_in.get(inb, []):
            p = self.pnrs[pid]
            if p["cur"] == out and p["bags"]:
                p["hot"] = out if on else None
                n += p["bags"]
        return n

    def tag_bags(self, inb: str, out: str, on: bool = True) -> int:
        n = 0
        for pid in self.by_in.get(inb, []):
            p = self.pnrs[pid]
            if p["cur"] == out and p["bags"]:
                p["rush"] = on
                n += p["bags"]
        return n

    def book_hotel(self, pid: str, rooms: int) -> dict:
        if self._fault("hotel", None):
            raise FeedDown("hotel partner API is not answering")
        if self.hotel["rooms"] < rooms:
            raise WriteRejected("hotel partner has no rooms left")
        self.hotel["rooms"] -= rooms
        self.hotel["bookings"][pid] = rooms
        return {"confirmation": f"H{abs(hash((self.seed, pid))) % 10 ** 6:06d}", "rooms": rooms}

    def cancel_hotel(self, pid: str) -> None:
        self.hotel["rooms"] += self.hotel["bookings"].pop(pid, 0)

    # ------------------------------------------------------------------ outcome metrics (same for baseline + system)
    def metrics(self) -> dict:
        fl = [x for x in self.legs.values() if x["kind"] in ("DEP", "ARR") and not x["cancelled"]]
        hub = [x for x in fl if x["kind"] == "DEP"]
        dis = [p for p in self.pnrs.values()
               if p["missed"] or p["rebooked_at"] is not None or p["status"] in ("MISCONNECTED", "CANCELLED")]
        waits = sorted(p["rebooked_at"] - p["risk_since"] for p in dis
                       if p["rebooked_at"] is not None and p["risk_since"] is not None for _ in range(p["size"]))
        dest_delay = 0
        for p in dis:
            end = self.legs[p["cur"]] if p["status"] in ("REBOOKED", "FLOWN") else self.legs["ND-" + self.legs[p["out"]]["dest"]]
            dest_delay += p["size"] * max(0, end["eta"] - self.legs[p["out"]]["sta"])
        special = [p for p in dis if p["ssr"]]
        n_dis = sum(p["size"] for p in dis)
        return {
            "departure_delay_min": sum(max(0, x["etd"] - x["std"]) for x in fl),
            "reactionary_delay_min": sum(reactionary(x) for x in fl),
            "hub_departure_delay_min": sum(max(0, x["etd"] - x["std"]) for x in hub),
            "hub_arrival_delay_min": sum(max(0, x["eta"] - x["sta"]) for x in fl if x["kind"] == "ARR"),
            "hub_otp15_pct": round(100 * sum(x["etd"] - x["std"] <= 15 for x in hub) / max(1, len(hub)), 1),
            "flights_delayed_15": sum(x["etd"] - x["std"] > 15 for x in hub),
            "cancelled_flights": sum(x["cancelled"] and x["kind"] == "DEP" for x in self.legs.values()),
            "missed_connections_pax": sum(p["size"] for p in dis if p["missed"]),
            "disrupted_pax": n_dis,
            "reaccommodated_pax": sum(p["size"] for p in dis if p["rebooked_at"] is not None),
            "not_reaccommodated_pax": sum(p["size"] for p in dis if p["status"] in ("MISCONNECTED", "CANCELLED")),
            "overnight_pax": sum(p["size"] for p in dis if p["overnight"] or p["status"] in ("MISCONNECTED", "CANCELLED")),
            "rebook_time_avg_min": round(sum(waits) / len(waits), 1) if waits else 0.0,
            "rebook_time_p90_min": waits[int(0.9 * (len(waits) - 1))] if waits else 0,
            "rebook_time_max_min": waits[-1] if waits else 0,
            "pax_dest_delay_avg_min": round(dest_delay / n_dis, 1) if n_dis else 0.0,
            "misconnected_bags": sum(p["bags_missed"] for p in self.pnrs.values()),
            "prm_umnr_disrupted": len(special),
            "prm_umnr_protected": sum(1 for p in special if p["rebooked_at"] is not None and p["risk_since"] is not None
                                      and p["rebooked_at"] - p["risk_since"] <= 30 and not p["overnight"]),
            "crew_legality_violations": self.crew_violations(),
        }

    def crew_violations(self) -> int:
        """Crew members whose flown duty ended past the limit (must always be 0)."""
        bad = 0
        for c in self.crew.values():
            flown = [(f, self.legs[f]["atd"], self.legs[f]["ata"]) for f in c["legs"] if self.legs[f]["ata"] is not None]
            if flown and not crew_legality.check(c, flown, self.now)["legal"]:
                bad += 1
        return bad
