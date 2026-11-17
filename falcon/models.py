"""Typed contracts shared by every agent, solver and stage (Pydantic)."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

REF_RX = re.compile(r"\b[A-HJ-NP-R]{6}\b")     # shape of a (fictional) booking reference; used by the privacy checks
Category = Literal["GROUND", "PASSENGER", "CREW", "AIRCRAFT", "SCHEDULE", "SAFETY"]
FeedStatus = Literal["OK", "STALE", "MISSING", "CONFLICT", "DOWN"]

# The ONLY action types the system can ever execute, with their fixed category.
# There is deliberately no "release aircraft" or "create sign-off" action.
ACTION_TYPES: dict[str, str] = {
    "GROUND_DISPATCH": "GROUND", "HOT_TRANSFER_BAGS": "GROUND", "PRIORITY_TAG_BAGS": "GROUND",
    "REROUTE_BAGS": "GROUND", "REASSIGN_STAND": "GROUND", "STAFF_ALERT": "GROUND",
    "REBOOK_GROUP": "PASSENGER", "MASS_REACCOMMODATION": "PASSENGER", "MEAL_VOUCHER": "PASSENGER",
    "BOOK_HOTEL": "PASSENGER", "SEND_MESSAGE": "PASSENGER",
    "SWAP_CREW": "CREW",
    "SWAP_AIRCRAFT": "AIRCRAFT",
    "HOLD_DEPARTURE": "SCHEDULE", "CANCEL_FLIGHT": "SCHEDULE", "RETIME_DEPARTURE": "SCHEDULE",
    "ESCALATE": "SAFETY",
}


class FeedDown(Exception):
    """A feed or partner API did not answer."""


class WriteRejected(Exception):
    """The write gateway (or the system of record) refused a write after re-checking live data."""


class DataRef(BaseModel):
    feed: str
    record_id: str
    fetched_at: int
    age_s: int = 0
    status: FeedStatus = "OK"
    snapshot_id: str = ""


class FeedResult(BaseModel):
    feed: str
    key: str
    value: Any = None
    fetched_at: int
    source: str
    status: FeedStatus
    age_s: int = 0
    snapshot_id: str = ""
    note: str = ""

    def ref(self) -> DataRef:
        return DataRef(feed=self.feed, record_id=self.key, fetched_at=self.fetched_at, age_s=self.age_s,
                       status=self.status, snapshot_id=self.snapshot_id)


class Undo(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class Action(BaseModel):
    action_id: str
    type: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    category: Category = "GROUND"
    reversible: bool = True
    undo: Undo | None = None
    est_cost_delta: float = 0.0
    est_delay_delta: float = 0.0
    agent: str = ""
    why: str = ""
    bundle: str = ""                  # actions of one bundle run as one saga
    requires: str = ""                # parent action id that must be approved and executed first
    inputs: list[str] = Field(default_factory=list)   # "feed:key" values this action depends on (G8)

    @model_validator(mode="after")
    def _known_type_and_fixed_category(self) -> "Action":
        if self.type not in ACTION_TYPES:
            raise ValueError(f"unknown action type {self.type!r}")
        self.category = ACTION_TYPES[self.type]      # nobody can relabel a crew change as GROUND
        return self

    def signature(self) -> str:
        """Stable identity of 'the same decision' across re-plans (used for approvals)."""
        p = self.params
        ids = p.get("pnrs") or p.get("rooms_by_pnr")
        if isinstance(ids, dict):            # block actions (vouchers, hotel, messages): identity = who is covered
            key = hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:8]
        else:
            key = next((p[k] for k in ("minutes", "to", "to_ac", "crew_in", "measure", "out", "pax", "reason")
                        if p.get(k)), "")
        return f"{self.type}|{self.target}|{key}"


class Proposal(BaseModel):
    case_id: str
    agent: str
    actions: list[Action] = Field(default_factory=list)
    rationale: str = ""
    data_refs: list[DataRef] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)   # solver tables, options, trade-off inputs
    llm_used: bool = False


class PlanStep(BaseModel):
    agent: str
    after: list[str] = Field(default_factory=list)


class GuardResult(BaseModel):
    action_id: str
    rule: str
    passed: bool
    detail: str = ""


# ---- feed record schemas (stage 2: format checks) ----
class MovementRec(BaseModel):
    fid: str
    kind: str
    std: int
    sta: int
    etd: int | None
    eta: int | None
    status: str


class InventoryRec(BaseModel):
    fid: str
    free_j: int = Field(ge=0)
    free_y: int = Field(ge=0)


def signoff_valid(aircraft: dict | None, now: int) -> bool:
    """An aircraft is usable only with a current maintenance release and no open tech-log item."""
    if not aircraft:
        return False
    s = aircraft.get("signoff")
    return bool(s and s.get("by") and s.get("valid_until", -1) > now and not aircraft.get("open_items"))
