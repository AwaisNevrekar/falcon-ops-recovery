"""Evaluation: the manual baseline vs the multi-agent system on the SAME replayed day.

Run:  .\\.venv\\Scripts\\python.exe -m falcon.evaluation
Writes docs/EVALUATION.md, docs/evaluation.json, docs/evaluation.png and docs/BUSINESS_CASE.md.

Both runs use the same seed, the same scripted events and the same physical rules. The system run uses the
rule-based fallbacks (no LLM) and simulated approvers so the comparison is reproducible to the minute.
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from . import baseline, business_case  # noqa: E402
from . import config as C  # noqa: E402
from .llm import LLM  # noqa: E402
from .store import loads  # noqa: E402
from .workflow import Engine  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
# key, label, unit, lower is better
METRICS = [
    ("departure_delay_min", "Total departure delay (all legs)", "min", True),
    ("reactionary_delay_min", "Reactionary (knock-on) delay", "min", True),
    ("flights_delayed_15", "Hub departures more than 15 min late", "flights", True),
    ("hub_otp15_pct", "Hub on-time performance (within 15 min)", "%", False),
    ("missed_connections_pax", "Missed passenger connections", "pax", True),
    ("overnight_pax", "Passengers stranded overnight", "pax", True),
    ("misconnected_bags", "Misconnected bags", "bags", True),
    ("rebook_time_avg_min", "Average time to re-accommodate a disrupted passenger", "min", True),
    ("rebook_time_p90_min", "90th-percentile time to re-accommodate", "min", True),
    ("mass_rebook_complete_min", "Cancelled flight: time until all 310 passengers have a new flight", "min", True),
    ("pax_dest_delay_avg_min", "Average delay at destination of disrupted passengers", "min", True),
    ("prm_umnr_protected", "PRM / unaccompanied minors rebooked within 30 min, same day, rule-compliant", "groups", False),
    ("crew_legality_violations", "Crew legality violations (must be 0)", "count", True),
]


def _mass_rebook(world) -> int:
    """Minutes from the cancellation of FG718 until its last passenger group has a new flight."""
    groups = [p for p in world.pnrs.values() if p["out"] == "FG718"]
    if any(p["rebooked_at"] is None for p in groups):
        return C.DAY_END - 875
    return max(p["rebooked_at"] for p in groups) - 875


def run_baseline(seed: int = C.SEED) -> dict:
    b = baseline.Baseline(seed)
    b.run_until(C.DAY_END)
    return {**b.results(), "mass_rebook_complete_min": _mass_rebook(b.world)}


def run_system(seed: int = C.SEED, llm=None) -> dict:
    e = Engine(approvers="simulated", llm=llm or LLM.off(), seed=seed)
    e.run_until(C.DAY_END)
    st = e.stats()
    done = e.store.q("SELECT type, params, decided_by FROM actions WHERE status='EXECUTED'")
    by_type: dict[str, int] = {}
    for a in done:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    waits: dict[str, list] = {}
    for r in e.store.q("SELECT role, requested_at, decided_at FROM approvals WHERE status IN ('APPROVED','REJECTED')"):
        waits.setdefault(r["role"], []).append(r["decided_at"] - r["requested_at"])
    dispatch_cost = sum(C.GROUND_MEASURES[loads(a["params"])["measure"]]["cost"] for a in done if a["type"] == "GROUND_DISPATCH")
    cases: dict[str, int] = {}
    for c in e.cases.values():
        cases[c["type"]] = cases.get(c["type"], 0) + 1
    return {**e.world.metrics(), "mass_rebook_complete_min": _mass_rebook(e.world), "stats": {
        **st, "by_type": dict(sorted(by_type.items())), "cases_by_type": cases, "ground_dispatch_cost": dispatch_cost,
        "hold_minutes": sum(loads(a["params"])["minutes"] for a in done if a["type"] == "HOLD_DEPARTURE"),
        "approval_wait_by_role": {C.ROLE_NAMES[k]: round(sum(v) / len(v), 1) for k, v in sorted(waits.items())},
        "bags_hot_transferred": sum(loads(a["params"])["bags"] for a in done if a["type"] == "HOT_TRANSFER_BAGS"),
        "pax_protected_by_holds": sum(loads(a["params"])["protects_pax"] for a in done if a["type"] == "HOLD_DEPARTURE"),
        "llm_mode": e.llm.describe()["mode"]}}


def evaluate(seed: int = C.SEED) -> dict:
    t0 = time.time()
    base = run_baseline(seed)
    t1 = time.time()
    system = run_system(seed)
    return {"meta": {"seed": seed, "day": "replayed operations day, 420 hub departures, 14 scripted disruptions",
                     "window": f"{C.hm(C.REPLAY_START)} to end of day", "baseline_seconds": round(t1 - t0, 1),
                     "system_seconds": round(time.time() - t1, 1),
                     "approver_response_min": {C.ROLE_NAMES[k]: v for k, v in C.SIM_APPROVER_DELAY_MIN.items()},
                     "manual": {"decision_min": C.MANUAL_DECISION_MIN, "agents": C.MANUAL_AGENTS,
                                "pax_per_agent_hour": C.MANUAL_REBOOK_PAX_PER_AGENT_HOUR}},
            "baseline": base, "system": system}


def change(b: float, s: float, lower_better: bool) -> str:
    if b == s:
        return "same"
    if b == 0:
        return f"{s - b:+g}"
    pct = 100 * (s - b) / b
    good = (pct < 0) == lower_better
    return f"{pct:+.0f}% {'(better)' if good else '(worse)'}"


def table_rows(res: dict) -> list[dict]:
    b, s = res["baseline"], res["system"]
    return [{"metric": label, "unit": unit, "baseline": b[k], "system": s[k], "change": change(b[k], s[k], low)}
            for k, label, unit, low in METRICS]


SHORT = {"departure_delay_min": "Total departure delay", "reactionary_delay_min": "Knock-on (reactionary) delay",
         "flights_delayed_15": "Hub departures over 15 min late", "missed_connections_pax": "Missed connections",
         "overnight_pax": "Passengers stranded overnight", "misconnected_bags": "Misconnected bags",
         "rebook_time_avg_min": "Average time to re-accommodate", "rebook_time_p90_min": "90th-percentile time to re-accommodate",
         "mass_rebook_complete_min": "Cancelled flight: all 310 rebooked", "pax_dest_delay_avg_min": "Average delay at destination"}


def chart_rows(res: dict) -> list[dict]:
    """System result as % of the manual baseline for every lower-is-better metric, best improvement first."""
    b, s = res["baseline"], res["system"]
    rows = [{"label": SHORT[k], "baseline": b[k], "system": s[k], "unit": unit, "pct": round(100 * s[k] / b[k], 1)}
            for k, _, unit, low in METRICS if low and b[k] and k in SHORT]
    return sorted(rows, key=lambda r: r["pct"])


def _chart(res: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    b, s = res["baseline"], res["system"]
    rows = chart_rows(res)
    fig, ax = plt.subplots(figsize=(9.5, 4.6), dpi=150)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    y = range(len(rows))
    ax.barh(y, [r["pct"] for r in rows], height=0.55, color="#2a78d6")
    ax.axvline(100, color="#898781", linewidth=1)
    ax.text(100, -0.8, "manual baseline = 100", color="#52514e", fontsize=8, ha="center")
    for i, r in enumerate(rows):                     # before → after, in one column to the right of the baseline line
        ax.text(104, i, f"{r['baseline']:g} → {r['system']:g} {r['unit']}", va="center", fontsize=8, color="#0b0b0b")
    ax.set_yticks(list(y), [r["label"] for r in rows], fontsize=8, color="#0b0b0b")
    ax.invert_yaxis()
    ax.set_xlim(0, 140)
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.set_xlabel("system result as % of the manual baseline (lower is better)", fontsize=8, color="#52514e")
    ax.tick_params(axis="x", labelsize=8, colors="#898781")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.set_title("Same replayed day: multi-agent system vs manual baseline", fontsize=10, loc="left", color="#0b0b0b")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write(res: dict, out: Path = DOCS) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    _chart(res, out / "evaluation.png")
    b, s, st, meta = res["baseline"], res["system"], res["system"]["stats"], res["meta"]
    lines = [
        "# Evaluation report — Falcon Gulf Ops Recovery", "",
        "*Generated by `python -m falcon.evaluation`. All data is fictional; all thresholds and costs are demo values.*", "",
        "## What was compared", "",
        f"The **same replayed operations day** (seed `{meta['seed']}`: 420 hub departures, about 67,000 passengers, the 14 "
        "scripted disruptions S1–S14 plus everything the sandstorm causes) was run twice, with the same physical rules "
        "(a flight cannot leave with an illegal crew or an unsigned aircraft in either run):", "",
        f"- **Baseline — manual operations.** No proactive holds, no turnaround compression, no hot transfers. Every "
        f"decision takes {meta['manual']['decision_min']} min after the problem is noticed; crew problems are noticed at "
        f"report time. Passengers are rebooked first-come-first-served by {meta['manual']['agents']} agents at "
        f"{meta['manual']['pax_per_agent_hour']} passengers per agent-hour. It is a competent manual operation: it makes "
        f"the same aircraft swaps as the system ({b['counts']['swaps']} of them), just later.",
        "- **System — the multi-agent workflow.** Rule-based fallbacks (no LLM), so the result is reproducible. "
        "Human approvals are answered by simulated approvers after "
        + ", ".join(f"{v} min ({k})" for k, v in list(meta["approver_response_min"].items())[:4])
        + "; one scripted request is never answered (S12).", "",
        "## Results", "", "| Metric | Unit | Manual baseline | System | Change |", "|---|---|---:|---:|---|"]
    lines += [f"| {r['metric']} | {r['unit']} | {r['baseline']:g} | {r['system']:g} | {r['change']} |" for r in table_rows(res)]
    lines += [
        "", "![System result as a percentage of the manual baseline](evaluation.png)", "",
        "## How the system got there", "",
        f"- **{st['cases']} disruption cases**, each in its own LangGraph thread "
        f"({', '.join(f'{n} {t}' for t, n in sorted(st['cases_by_type'].items()))}).",
        f"- **{st['actions_executed']} actions executed**: {st['actions_auto']} automatically (ground, baggage, individual "
        f"rebooking inside every rule) and {st['actions_approved']} under a human approval record.",
        f"- **{st['approvals_requested']} approval requests**, average wait {st['approval_wait_avg_min']} min "
        f"({', '.join(f'{k}: {v} min' for k, v in st['approval_wait_by_role'].items())}); "
        f"{st['approvals_timed_out']} timed out and took the safe default before being escalated.",
        f"- **Trade-offs taken:** {st['by_type'].get('HOLD_DEPARTURE', 0)} holds totalling {st['hold_minutes']} min protected "
        f"{st['pax_protected_by_holds']} connecting passengers; {st['by_type'].get('SWAP_AIRCRAFT', 0)} aircraft swaps; "
        f"{st['by_type'].get('GROUND_DISPATCH', 0)} extra ground-team dispatches (AED {st['ground_dispatch_cost']:,}); "
        f"{st['by_type'].get('HOT_TRANSFER_BAGS', 0)} hot transfers moved {st['bags_hot_transferred']} bags.",
        f"- **Data quality:** {st['actions_held_bad_data']} actions were held because an input was stale, conflicting or "
        f"unavailable, and resumed when it was reliable again. {st['escalations']} escalations went to humans.",
        f"- **Recovery:** {st['saga_undos']} saga steps were undone after a failed step, then re-planned on fresh data.",
        "", "## Safety outcome", "",
        f"- Crew legality violations: **{s['crew_legality_violations']}** (baseline {b['crew_legality_violations']}).",
        f"- Actions blocked by a hard guard rule in the live run: {st['actions_blocked_by_guard']} — the agents only ever "
        "proposed legal crew and signed-off aircraft; the guards are proven by tests that try to break each rule "
        "(`tests/test_guards.py`, `tests/test_saga.py`) and by `scenarios.py`.",
        "- No crew, aircraft or schedule change executed without the right human approval record "
        "(checked in `outputs/test_log.md`, row SAFETY).",
        "", "## What this does and does not show", "",
        "- **Biggest gains:** knock-on delay (turnaround compression and earlier swaps), time to re-accommodate "
        "(solver in minutes vs a transfer-desk queue), bags (hot transfers) and priority passengers (the fairness table).",
        f"- **Smallest gain:** missed connections ({b['missed_connections_pax']} → {s['missed_connections_pax']}). Most "
        f"misconnections on this day need a hold longer than the {C.HOLD_MAX_MIN}-minute limit or cost more than they "
        "save, so the system rebooks those passengers quickly instead of holding aircraft for them.",
        "- **Assumptions that drive the numbers:** the approver response times above, the manual decision time and "
        "desk rate, and the cost model in `falcon/config.py`. Change them and re-run to see the sensitivity.",
        "- **Limits of the model:** one seeded day; estimates in the replay are exact (real ETAs are noisier); ground "
        "teams and transfer vehicles are simple half-hour pools; outstation passengers are out of scope. "
        "The result is evidence that the design works end to end, not a forecast for a real airline.",
        "", "## Reproduce", "", "```powershell", r".\.venv\Scripts\python.exe -m falcon.evaluation",
        r".\.venv\Scripts\python.exe scenarios.py", "```",
        f"\nRun time: baseline {meta['baseline_seconds']} s, system {meta['system_seconds']} s."]
    (out / "EVALUATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    business_case.write_report(res, out)
    return out / "EVALUATION.md"


def load(out: Path = DOCS) -> dict | None:
    try:
        return json.loads((out / "evaluation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = evaluate()
    path = write(result)
    print(f"{'metric':<76}{'baseline':>10}{'system':>10}  change")
    for r in table_rows(result):
        print(f"{r['metric']:<76}{r['baseline']:>10g}{r['system']:>10g}  {r['change']}")
    bc = business_case.compute(business_case.defaults(), result)
    print(f"\nBusiness case (default assumptions): AED {bc['per_day']['total']:,.0f} per disrupted day, "
          f"AED {bc['per_year']['net']:,.0f} net per year, payback {bc['payback_months']:.1f} months")
    print("written:", path, "and", path.parent / "BUSINESS_CASE.md")
