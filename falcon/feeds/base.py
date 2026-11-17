"""Feed adapter base: freshness stamp, timeout, retry with backoff and a circuit breaker for every call."""
from __future__ import annotations

import json
import time

from pydantic import ValidationError

from .. import config as C
from ..models import FeedDown, FeedResult


class CircuitBreaker:
    """CLOSED -> (N failed calls) -> OPEN -> (cooldown) -> HALF_OPEN -> one probe -> CLOSED or OPEN again."""

    def __init__(self, fails: int = C.BREAKER_FAILS, cooldown_min: int = C.BREAKER_COOLDOWN_MIN):
        self.fails_needed, self.cooldown = fails, cooldown_min
        self.state, self.fails, self.opened_at = "CLOSED", 0, None

    def allow(self, now: int) -> bool:
        if self.state == "OPEN":
            if now - self.opened_at < self.cooldown:
                return False
            self.state = "HALF_OPEN"
        return True

    def success(self) -> None:
        self.state, self.fails = "CLOSED", 0

    def failure(self, now: int) -> None:
        self.fails += 1
        if self.state == "HALF_OPEN" or self.fails >= self.fails_needed:
            self.state, self.opened_at = "OPEN", now


class FeedAdapter:
    name = ""
    source = "mock"
    schema = None          # optional Pydantic model for single records

    def __init__(self, world, store=None, sleep=None):
        self.world, self.store = world, store
        self.sleep = sleep or (lambda seconds: None)      # the replay never really sleeps between retries
        self.breaker = CircuitBreaker()
        self.stats = {"calls": 0, "retries": 0, "failures": 0, "last_status": "OK", "last_at": None}
        self.issues: dict[str, tuple] = {}                # key -> (status, age_s, seen_at)

    def call(self, fn, *args):
        """Run one partner call with timeout, retries (exponential backoff) and the circuit breaker."""
        now = self.world.now
        if not self.breaker.allow(now):
            raise FeedDown(f"{self.name}: circuit open (failing fast)")
        last = None
        for attempt in range(C.RETRY_MAX + 1):
            self.stats["calls"] += 1
            try:
                t0 = time.monotonic()
                out = fn(*args)
                if time.monotonic() - t0 > C.PARTNER_TIMEOUT_S:
                    raise TimeoutError(f"{self.name} answered after the {C.PARTNER_TIMEOUT_S}s timeout")
                self.breaker.success()
                return out
            except (FeedDown, TimeoutError) as e:
                last = e
                if attempt < C.RETRY_MAX:
                    self.stats["retries"] += 1
                    self.sleep(C.RETRY_BACKOFF_S * 2 ** attempt)
        self.stats["failures"] += 1
        self.breaker.failure(now)
        raise FeedDown(f"{self.name}: {last}")

    def get(self, key: str = "*", snapshot: bool = True) -> FeedResult:
        now = self.world.now
        limit = C.STALE_AFTER_S.get(self.name)
        try:
            value, updated = self.call(self.world.read, self.name, key)
        except FeedDown as e:
            return self._result(key, None, now, "DOWN", 0, str(e), snapshot)
        except (KeyError, IndexError):
            return self._result(key, None, now, "MISSING", 0, f"unknown record {key!r}", snapshot)
        age = max(0, (now - updated) * 60)
        status, note = "OK", ""
        if value is None:
            status, note = "MISSING", "no record"
        elif limit and age > limit:
            status, note = "STALE", f"last update {age // 60} min ago (limit {limit // 60} min)"
        elif self.schema is not None and isinstance(value, dict) and key != "__hb__":
            try:
                self.schema.model_validate(value)
            except ValidationError as e:
                status, note = "MISSING", f"schema check failed: {e.errors()[0]['loc']}"
        if isinstance(value, list) and limit:              # bulk read: stamp each record's own freshness
            for rec in value:
                if isinstance(rec, dict) and "upd" in rec:
                    rec_age = (now - rec["upd"]) * 60
                    rec["feed_status"] = "STALE" if rec_age > limit else "OK"
                    if rec["feed_status"] != "OK":
                        self.issues[rec.get("fid", "?")] = ("STALE", rec_age, now)
        return self._result(key, value, now, status, age, note, snapshot)

    def _result(self, key, value, now, status, age, note, snapshot) -> FeedResult:
        sid = f"{self.name}:{key}@{now}"
        self.stats["last_status"], self.stats["last_at"] = status, now
        if status != "OK":
            self.issues[key] = (status, age, now)
        else:
            self.issues.pop(key, None)
        if snapshot and self.store is not None:
            try:
                text = json.dumps(value, default=str, ensure_ascii=False)[:4000]
            except (TypeError, ValueError):
                text = "<not serialisable>"
            self.store.add("feed_snapshots", snapshot_id=sid, feed=self.name, key=key, fetched_at=now, age_s=age,
                           status=status, source=self.source, value=text)
        return FeedResult(feed=self.name, key=key, value=value, fetched_at=now, source=self.source, status=status,
                          age_s=age, snapshot_id=sid, note=note)
