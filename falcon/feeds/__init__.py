"""Feed hub: one place to read every feed, and the live feed-health view."""
from __future__ import annotations

from .. import config as C
from ..models import FeedResult
from .adapters import ALL_FEEDS


class FeedHub:
    def __init__(self, world, store=None):
        self.world, self.store = world, store
        self.adapters = {cls.name: cls(world, store) for cls in ALL_FEEDS}

    def __getitem__(self, feed: str):
        return self.adapters[feed]

    def get(self, feed: str, key: str = "*", snapshot: bool = True) -> FeedResult:
        return self.adapters[feed].get(key, snapshot)

    def status_of(self, ref: str) -> FeedResult:
        """Live status of a 'feed:key' reference (used by guard G8 right before acting)."""
        feed, _, key = ref.partition(":")
        return self.get(feed, key or "*", snapshot=False)

    def health(self) -> list[dict]:
        """One row per feed: status, age, breaker state and the records currently flagged."""
        rows, now = [], self.world.now
        for name, a in self.adapters.items():
            probe = a.get("__hb__", snapshot=False)
            issues = {k: v for k, v in a.issues.items() if k != "__hb__" and now - v[2] <= 2}
            status = probe.status if probe.status != "OK" else ("DEGRADED" if issues else "OK")
            limit = C.STALE_AFTER_S.get(name)
            rows.append({
                "feed": name, "source": a.source, "status": status, "age_s": probe.age_s,
                "stale_after_s": limit, "breaker": a.breaker.state, "calls": a.stats["calls"],
                "retries": a.stats["retries"], "failures": a.stats["failures"], "note": probe.note,
                "records_flagged": ", ".join(f"{k} {v[0]} ({v[1] // 60} min old)" for k, v in sorted(issues.items())),
            })
        return rows
