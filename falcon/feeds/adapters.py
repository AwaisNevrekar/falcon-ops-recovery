"""One adapter per feed / partner API (ARCHITECTURE.md section 3). All mocked and replayed from the ops day.

Each adapter inherits the same protection from FeedAdapter: a freshness stamp on every value, a timeout,
retry with backoff and its own circuit breaker — so one failing partner never drags another feed down.
"""
from __future__ import annotations

from ..models import InventoryRec, MovementRec
from .base import FeedAdapter
from .validate import sanitize_notes


class ScheduleFeed(FeedAdapter):
    """Flights, rotations, aircraft type, STD/STA. Static per day."""
    name, source = "schedule", "airline schedule system (mock)"


class MovementsFeed(FeedAdapter):
    """OOOI times and airline ETAs. Can arrive late, go stale or disagree with ATC."""
    name, source, schema = "movements", "airline movement messages (mock)", MovementRec


class AtcFeed(FeedAdapter):
    """ATC / flow-management ETA and slot delay."""
    name, source = "atc", "ATC flow ETA (mock)"


class TurnaroundFeed(FeedAdapter):
    """Per-task turnaround progress, ground teams and free-text ops notes (untrusted text!).

    Stage 2 for free text lives here: notes are scanned for prompt injection before the value is stored
    in a snapshot, shown on screen or handed to any agent. Blocked text never leaves this adapter.
    """
    name, source = "turnaround", "ground handler turnaround system (mock)"
    guard = None                      # optional guard-model callable (llama-guard3 / Prompt Guard)

    def __init__(self, world, store=None, sleep=None):
        super().__init__(world, store, sleep)
        self.blocked: dict[str, dict] = {}        # flight -> what was blocked (patterns only, never the text)

    def _clean(self, key, value):
        if isinstance(value, dict) and value.get("notes"):
            safe, blocked = sanitize_notes(value["notes"], self.guard)
            if blocked:
                self.blocked.setdefault(key, {"flight": key, "first_seen": self.world.now, "count": len(blocked),
                                              "patterns": sorted({p for b in blocked for p in b["patterns"]})})
            value = {**value, "notes": safe}
        return value


class StandsFeed(FeedAdapter):
    """Stand and gate allocation, compatibility, occupancy and double allocations."""
    name, source = "stands", "airport stand allocation (mock)"


class CrewFeed(FeedAdapter):
    """Crew roster, pairings and duty clock."""
    name, source = "crew", "crew management system (mock)"


class MaintenanceFeed(FeedAdapter):
    """Tech-log items and release sign-off (who/when). Read-only: the system can never write a sign-off."""
    name, source = "maintenance", "maintenance control system (mock)"


class PaxFeed(FeedAdapter):
    """Passenger groups with HASHED ids, itineraries, connections and SSR codes. No names, no booking refs."""
    name, source = "pax", "reservations / PNR (mock, hashed ids)"


class BaggageFeed(FeedAdapter):
    """Bag-to-flight mapping and transfer status."""
    name, source = "baggage", "baggage messages (mock)"


class InventoryFeed(FeedAdapter):
    """Free seats per flight and cabin. Changes while we plan, so it is re-read at execution."""
    name, source, schema = "inventory", "seat inventory (mock)", InventoryRec


class HotelPartnerAPI(FeedAdapter):
    """External hotel partner: availability and bookings. Times out and has outages."""
    name, source = "hotel", "hotel partner API (mock)"

    def book(self, pax_hash: str, rooms: int) -> dict:
        return self.call(self.world.book_hotel, pax_hash, rooms)

    def cancel(self, pax_hash: str) -> None:
        self.world.cancel_hotel(pax_hash)


class WeatherFeed(FeedAdapter):
    """METAR-style weather and arrival-rate reduction."""
    name, source = "weather", "weather service (mock)"


ALL_FEEDS = [ScheduleFeed, MovementsFeed, AtcFeed, TurnaroundFeed, StandsFeed, CrewFeed, MaintenanceFeed, PaxFeed,
             BaggageFeed, InventoryFeed, HotelPartnerAPI, WeatherFeed]
