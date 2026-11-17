"""Every threshold, limit and cost figure in one place.

All values are DEMO values for a fictional airline. They are configurable and are not a legal,
regulatory or commercial statement. Times are minutes unless the name says otherwise. Money is AED.
"""

SEED = 20261117
HUB = "FGH"
N_DEPARTURES = 420
REPLAY_START = 13 * 60 + 45        # simulated 13:45
REPLAY_END = 20 * 60               # demo window ends 20:00
DAY_END = 27 * 60                  # evaluation runs until every flight of the day has flown

# ---------------------------------------------------------------- fleet
FLEET = {
    "NB": {"seats_j": 12, "seats_y": 168, "min_turn": 45, "floor_turn": 33, "out_turn": 40},
    "WB": {"seats_j": 30, "seats_y": 290, "min_turn": 65, "floor_turn": 50, "out_turn": 60},
}
SPARES = [("FG-N901", "NB"), ("FG-W902", "WB"), ("FG-N903", "NB")]
SWAP_PREP_MIN = 30                 # tow, load and brief a spare aircraft
SWAP_MIN_INBOUND_DELAY = 45        # only consider a swap for an inbound this late
SWAP_MIN_SAVING_MIN = 60           # ... and only if it saves this many delay minutes downstream

# ---------------------------------------------------------------- feeds and partners
STALE_AFTER_S = {
    "schedule": None, "movements": 300, "atc": 300, "turnaround": 180, "stands": 300, "crew": 600,
    "maintenance": 600, "pax": 900, "baggage": 300, "inventory": 120, "hotel": None, "weather": 1800,
}
PARTNER_TIMEOUT_S = 3.0
RETRY_MAX = 2
RETRY_BACKOFF_S = 0.5
BREAKER_FAILS = 3                  # consecutive failed calls before the circuit opens
BREAKER_COOLDOWN_MIN = 5           # simulated minutes before a half-open probe
ETA_CONFLICT_MIN = 10              # airline ETA vs ATC ETA disagreement that counts as a conflict
ARRIVAL_RATE_PER_H = 21            # the hub runs close to its normal arrival capacity
STORM_RECOVERY_FACTOR = 1.25       # after the storm ATC drains the queue faster than the normal rate

# ---------------------------------------------------------------- crew (demo limits)
MAX_DUTY_MIN = 780
MAX_SECTORS = 6
MIN_REST_MIN = 720
POST_FLIGHT_MIN = 30
CREW_REPORT_MIN = 60
CREW_CONNECT_MIN = 30              # crew changing aircraft at the hub
STANDBY_CALLOUT_MIN = 45

# ---------------------------------------------------------------- passengers and bags
MCT_PAX_MIN = 35
MCT_BAG_MIN = 45
HOT_TRANSFER_MIN = 25              # tail-to-tail transfer beats the normal bag MCT
HOT_TRANSFER_RUNS_PER_30MIN = 12
HOT_TRANSFER_BAGS_PER_RUN = 40
HOT_TRANSFER_MIN_BAGS = 4          # fewer bags than this get a priority tag instead of a vehicle (unless PRM/UMNR)
TIGHT_CONNECTION_MIN = 60
LONG_DELAY_MIN = 240
REBOOK_BUFFER_MIN = 10             # extra margin on top of MCT when rebooking
AUTO_REBOOK_MAX_PAX = 50           # above this per flight -> Customer Service Lead
MEAL_VOUCHER_AFTER_MIN = 120
MEAL_VOUCHER_AED = 60
VOUCHER_MAX_AED = 150              # hard cap per passenger (G7)
HOTEL_RATE_AED = 420
HOTEL_MAX_RATE_AED = 600           # hard cap per room (G7)
HOTEL_AUTO_BUDGET_AED = 12000      # per case; above -> Customer Service Lead
HOTEL_ROOMS = 400

# ---------------------------------------------------------------- schedule and ground
LATE_INBOUND_MIN = 15              # inbound delay that opens a disruption case
CASE_REOPEN_MIN = 10               # delay growth that re-opens a handled flight
HOLD_AUTO_MAX_MIN = 5              # longer holds need the Duty Ops Controller
HOLD_MAX_MIN = 20
GROUND_MEASURES = {                # saves = minutes off the turnaround, pool = extra teams per half hour of work
    "extra_cleaning_team": {"saves": 5, "cost": 350, "pool": 8},
    "priority_catering": {"saves": 4, "cost": 250, "pool": 6},
    "priority_fuelling": {"saves": 3, "cost": 200, "pool": 6},
    "extra_baggage_loaders": {"saves": 4, "cost": 300, "pool": 8},
}                                  # pools belong to the half hour in which the turnaround itself happens
STAND_BUFFER_MIN = 5
STAND_HORIZON_MIN = 90
STAND_WAIT_MAX_MIN = 8             # an arriving aircraft waits this long for a blocked stand

# ---------------------------------------------------------------- approvals
DUTY_OPS, CREW_CTRL, MAINT, CS_LEAD = "DUTY_OPS", "CREW_CONTROLLER", "MAINTENANCE", "CS_LEAD"
ROLE_NAMES = {
    DUTY_OPS: "Duty Ops Controller", CREW_CTRL: "Crew Controller", MAINT: "Maintenance Control",
    CS_LEAD: "Customer Service Lead", "OPS_MANAGER": "Ops Duty Manager (escalation)",
    "CREW_MANAGER": "Crew Duty Manager (escalation)", "CS_MANAGER": "Customer Service Manager (escalation)",
    "MX_MANAGER": "Maintenance Duty Manager (escalation)",
}
APPROVAL_TIMEOUT_MIN = {DUTY_OPS: 10, CREW_CTRL: 10, MAINT: 15, CS_LEAD: 12,
                        "OPS_MANAGER": 20, "CREW_MANAGER": 20, "CS_MANAGER": 20, "MX_MANAGER": 20}
ESCALATION_ROLE = {DUTY_OPS: "OPS_MANAGER", CREW_CTRL: "CREW_MANAGER", CS_LEAD: "CS_MANAGER", MAINT: "MX_MANAGER"}
SIM_APPROVER_DELAY_MIN = {DUTY_OPS: 4, CREW_CTRL: 5, MAINT: 8, CS_LEAD: 6,
                          "OPS_MANAGER": 6, "CREW_MANAGER": 6, "CS_MANAGER": 6, "MX_MANAGER": 6}
MAX_CASE_ITERATIONS = 6

# ---------------------------------------------------------------- cost model (trade-off scoring)
COST = {
    "delay_min": {"NB": 310, "WB": 560},   # aircraft delay minute
    "pax_delay_min": 1.4,                  # passenger goodwill per minute
    "misconnect_pax": 950,                 # rebooking + care + compensation exposure
    "overnight_pax": 620,                  # hotel, meals, transfers
    "bag_misconnect": 380,
    "crew_callout": 2500,
    "aircraft_swap": 6000,
    "stand_change": 150,
    "tow": 900,
}

# ---------------------------------------------------------------- baseline (manual operations)
MANUAL_DECISION_MIN = 25
MANUAL_REBOOK_PAX_PER_AGENT_HOUR = 12
MANUAL_AGENTS = 12

# ---------------------------------------------------------------- LLM
LLM_TIMEOUT_S = 20
LLM_MAX_CALLS = 300                # per run; after that every step uses its rule fallback


def hm(minute: int | None) -> str:
    """Simulated minute -> 'HH:MM' (adds +1 for the next day)."""
    if minute is None:
        return "--:--"
    return f"{(minute // 60) % 24:02d}:{minute % 60:02d}" + ("+1" if minute >= 1440 else "")
