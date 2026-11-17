"""Business-case model: delay costs avoided, compensation reduced, staff hours saved.

Every input is an explicit, editable assumption (demo values for a fictional airline — replace them with
real figures). The physical quantities (delay minutes, passengers, bags, decisions) come from the
evaluation run of baseline vs system on the same day; this module only prices them.
Results are split between the AIRLINE and its GROUND-HANDLING PARTNER.
"""
from __future__ import annotations

from pathlib import Path

from . import config as C

# key -> (group, label, default, unit, why this value)
ASSUMPTIONS = {
    "delay_cost_per_min": ("Airline", "Cost of one aircraft delay minute", 380, "AED/min",
                           "blended narrow/wide-body: crew, fuel burn, maintenance, lost utilisation"),
    "misconnect_cost_per_pax": ("Airline", "Cost of one missed connection", 950, "AED/pax",
                                "rebooking, compensation exposure, lost goodwill"),
    "overnight_cost_per_pax": ("Airline", "Care for one passenger stranded overnight", 620, "AED/pax",
                               "hotel, meals, transfers"),
    "bag_cost": ("Airline", "Cost of one misconnected bag", 380, "AED/bag", "courier delivery, handling, claims"),
    "agent_hourly_cost": ("Airline", "Customer-service agent, fully loaded", 85, "AED/h", ""),
    "controller_hourly_cost": ("Airline", "Operations controller, fully loaded", 160, "AED/h", ""),
    "review_min_per_approval": ("Airline", "Controller time to review and answer one approval request", 3, "min",
                                "with the trade-off and data already on screen"),
    "review_min_per_escalation": ("Airline", "Staff time to handle one escalation from the system", 10, "min", ""),
    "sla_penalty": ("Ground handler", "SLA penalty per departure more than 15 min late", 2500, "AED/flight",
                    "share attributable to the turnaround"),
    "overtime_per_delay_min": ("Ground handler", "Overtime and idle equipment per hub departure delay minute", 45,
                               "AED/min", "teams and equipment held on a late stand"),
    "extra_team_cost_factor": ("Ground handler", "Cost of extra teams dispatched by the system (multiplier)", 1.0, "x",
                               "1.0 = the per-dispatch costs in config.GROUND_MEASURES"),
    "disrupted_days_per_year": ("Scale", "Days per year like the replayed day", 40, "days", "sandstorm, fog, ATC, strikes"),
    "ordinary_days_per_year": ("Scale", "Ordinary operating days per year", 325, "days", ""),
    "ordinary_day_share": ("Scale", "Benefit on an ordinary day as a share of a disrupted day", 0.08, "share",
                           "everyday late inbounds and tight connections"),
    "run_cost_per_year": ("Investment", "Platform, support, integrations and LLM usage per year", 1_500_000, "AED/yr", ""),
    "implementation_cost": ("Investment", "One-off implementation and integration", 3_000_000, "AED", ""),
}


def defaults() -> dict[str, float]:
    return {k: v[2] for k, v in ASSUMPTIONS.items()}


def compute(a: dict[str, float], res: dict) -> dict:
    """Price the baseline-vs-system differences. Positive = money saved by the system."""
    b, s = res["baseline"], res["system"]
    st = s.get("stats", {})
    agent_h_base = b.get("agent_hours", b["reaccommodated_pax"] / C.MANUAL_REBOOK_PAX_PER_AGENT_HOUR)
    ctrl_h_base = b.get("controller_hours", 0.0)
    ctrl_h_sys = st.get("approvals_requested", 0) * a["review_min_per_approval"] / 60
    agent_h_sys = st.get("escalations", 0) * a["review_min_per_escalation"] / 60
    airline = {
        "Delay cost avoided": (b["departure_delay_min"] - s["departure_delay_min"]) * a["delay_cost_per_min"],
        "Missed-connection cost avoided": (b["missed_connections_pax"] - s["missed_connections_pax"]) * a["misconnect_cost_per_pax"],
        "Overnight care avoided": (b["overnight_pax"] - s["overnight_pax"]) * a["overnight_cost_per_pax"],
        "Baggage misconnect cost avoided": (b["misconnected_bags"] - s["misconnected_bags"]) * a["bag_cost"],
        "Rebooking agent hours saved": (agent_h_base - agent_h_sys) * a["agent_hourly_cost"],
        "Controller hours saved": (ctrl_h_base - ctrl_h_sys) * a["controller_hourly_cost"],
    }
    handler = {
        "SLA penalties avoided": (b["flights_delayed_15"] - s["flights_delayed_15"]) * a["sla_penalty"],
        "Overtime and idle equipment avoided": (b["hub_departure_delay_min"] - s["hub_departure_delay_min"]) * a["overtime_per_delay_min"],
        "Extra ground teams dispatched (cost)": -st.get("ground_dispatch_cost", 0) * a["extra_team_cost_factor"],
    }
    day_air, day_gh = sum(airline.values()), sum(handler.values())
    day_total = day_air + day_gh
    days = a["disrupted_days_per_year"] + a["ordinary_days_per_year"] * a["ordinary_day_share"]
    gross = day_total * days
    net = gross - a["run_cost_per_year"]
    return {
        "airline": airline, "handler": handler,
        "per_day": {"airline": day_air, "handler": day_gh, "total": day_total},
        "per_year": {"airline": day_air * days, "handler": day_gh * days, "gross": gross, "net": net,
                     "equivalent_disrupted_days": days},
        "staff_hours_per_day": {"agents_baseline": agent_h_base, "agents_system": agent_h_sys,
                                "controllers_baseline": ctrl_h_base, "controllers_system": ctrl_h_sys,
                                "saved": agent_h_base - agent_h_sys + ctrl_h_base - ctrl_h_sys},
        "payback_months": (a["implementation_cost"] / (net / 12)) if net > 0 else float("inf"),
        "roi_3y_pct": 100 * (3 * net - a["implementation_cost"]) / a["implementation_cost"] if a["implementation_cost"] else 0.0,
        "quantities": {"delay_min": b["departure_delay_min"] - s["departure_delay_min"],
                       "missed_pax": b["missed_connections_pax"] - s["missed_connections_pax"],
                       "overnight_pax": b["overnight_pax"] - s["overnight_pax"],
                       "bags": b["misconnected_bags"] - s["misconnected_bags"],
                       "late_flights": b["flights_delayed_15"] - s["flights_delayed_15"],
                       "hub_delay_min": b["hub_departure_delay_min"] - s["hub_departure_delay_min"]},
    }


def explain(a: dict[str, float], bc: dict) -> dict[str, str]:
    """How each line is calculated: quantity from the evaluation × the assumption next to it."""
    q, sh = bc["quantities"], bc["staff_hours_per_day"]
    return {
        "Delay cost avoided": f"{q['delay_min']} delay min × AED {a['delay_cost_per_min']:g}",
        "Missed-connection cost avoided": f"{q['missed_pax']} passengers × AED {a['misconnect_cost_per_pax']:g}",
        "Overnight care avoided": f"{q['overnight_pax']} passengers × AED {a['overnight_cost_per_pax']:g}",
        "Baggage misconnect cost avoided": f"{q['bags']} bags × AED {a['bag_cost']:g}",
        "Rebooking agent hours saved": f"{sh['agents_baseline'] - sh['agents_system']:.1f} h × AED {a['agent_hourly_cost']:g}",
        "Controller hours saved": f"{sh['controllers_baseline'] - sh['controllers_system']:.1f} h × AED {a['controller_hourly_cost']:g}",
        "SLA penalties avoided": f"{q['late_flights']} flights × AED {a['sla_penalty']:g}",
        "Overtime and idle equipment avoided": f"{q['hub_delay_min']} hub delay min × AED {a['overtime_per_delay_min']:g}",
        "Extra ground teams dispatched (cost)": "extra teams the system called out (a cost to the handler)",
    }


def write_report(res: dict, out: Path) -> Path:
    a = defaults()
    bc = compute(a, res)
    sh, basis = bc["staff_hours_per_day"], explain(a, bc)
    lines = [
        "# Business case — Falcon Gulf Ops Recovery", "",
        "*Generated by `python -m falcon.evaluation` from `docs/evaluation.json`. Every figure below is "
        "**quantity from the evaluation × an editable assumption**. The assumptions are demo values for a fictional "
        "airline: replace them with real ones in the dashboard (Business case tab) or in `falcon/business_case.py`.*", "",
        "## Headline (default assumptions)", "",
        f"- **AED {bc['per_day']['total']:,.0f} saved on one disrupted day** — airline AED {bc['per_day']['airline']:,.0f}, "
        f"ground handler AED {bc['per_day']['handler']:,.0f}.",
        f"- **{sh['saved']:.0f} staff hours saved that day** (rebooking agents {sh['agents_baseline']:.0f} → "
        f"{sh['agents_system']:.1f} h, controllers {sh['controllers_baseline']:.1f} → {sh['controllers_system']:.1f} h).",
        f"- **AED {bc['per_year']['gross']:,.0f} gross per year** over {bc['per_year']['equivalent_disrupted_days']:.0f} "
        f"disrupted-day equivalents; **AED {bc['per_year']['net']:,.0f} net** after running costs.",
        f"- **Payback {bc['payback_months']:.1f} months**, three-year ROI {bc['roi_3y_pct']:.0f}%.", "",
        "## Airline — per disrupted day", "", "| Line | How it is calculated | AED |", "|---|---|---:|"]
    lines += [f"| {k} | {basis[k]} | {v:,.0f} |" for k, v in bc["airline"].items()]
    lines += [f"| **Total airline** | | **{bc['per_day']['airline']:,.0f}** |", "",
              "## Ground-handling partner — per disrupted day", "", "| Line | How it is calculated | AED |", "|---|---|---:|"]
    lines += [f"| {k} | {basis[k]} | {v:,.0f} |" for k, v in bc["handler"].items()]
    lines += [f"| **Total ground handler** | | **{bc['per_day']['handler']:,.0f}** |", "",
              "## Per year", "", "| | AED |", "|---|---:|",
              f"| Airline benefit | {bc['per_year']['airline']:,.0f} |",
              f"| Ground-handler benefit | {bc['per_year']['handler']:,.0f} |",
              f"| Running cost | -{a['run_cost_per_year']:,.0f} |",
              f"| **Net benefit per year** | **{bc['per_year']['net']:,.0f}** |",
              f"| One-off implementation | {a['implementation_cost']:,.0f} |", "",
              "## Assumptions (all editable)", "", "| Group | Assumption | Value | Unit | Note |", "|---|---|---:|---|---|"]
    lines += [f"| {g} | {label} | {a[k]:,g} | {unit} | {note} |" for k, (g, label, _, unit, note) in ASSUMPTIONS.items()]
    lines += ["", "## Read this before quoting the numbers", "",
              "- The quantities come from **one seeded, simulated day**; the prices are **placeholders**. The model's value is "
              "its structure: change any assumption and every figure updates.",
              "- The ground handler pays for the extra teams the system dispatches but gains more from fewer SLA penalties "
              "and less overtime; whether that split is fair is a contract question, shown here so it can be discussed.",
              "- Not counted: revenue protected by better punctuality, crew overtime, fuel from fewer holds in the air, "
              "and the cost of the human approvers' attention beyond the review minutes above."]
    path = out / "BUSINESS_CASE.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
