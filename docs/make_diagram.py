"""Draws the architecture diagram: agent design, API integrations, state management, human control points.

Run:  .\\.venv\\Scripts\\python.exe docs\\make_diagram.py      (writes docs/architecture.png and docs/architecture.pdf)
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

INK, MUTED, BLUE, RED, LINE = "#0b0b0b", "#6b6a66", "#1c5cab", "#b3261e", "#52514e"
fig, ax = plt.subplots(figsize=(17, 22))
ax.set_xlim(0, 103)
ax.set_ylim(0, 132)
ax.axis("off")
fig.patch.set_facecolor("white")


def box(x, y, w, h, title, who="", lines=(), tools=(), fc="white", ec="#898781", lw=1.3, tc=INK, size=10.5):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.9", fc=fc, ec=ec, lw=lw))
    ty = y + h - 1.0
    ax.text(x + 1.0, ty, title, fontsize=size + 1, fontweight="bold", color=tc, va="top")
    ty -= 2.1
    if who:
        ax.text(x + 1.0, ty, who, fontsize=size - 1, color=BLUE, va="top", style="italic")
        ty -= 1.9
    for ln in lines:
        ax.text(x + 1.0, ty, ln, fontsize=size - 1, color=INK, va="top")
        ty -= 1.75
    for ln in tools:
        ax.text(x + 1.0, ty, ln, fontsize=size - 2, color=MUTED, va="top")
        ty -= 1.6


def arrow(pts, label="", lx=0.0, ly=0.0, color=LINE, dashed=False, ha="left"):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    style = (0, (4, 3)) if dashed else "solid"
    if len(pts) > 2:
        ax.plot(xs[:-1], ys[:-1], color=color, lw=1.4, ls=style, solid_capstyle="round")
    ax.annotate("", xy=pts[-1], xytext=pts[-2], arrowprops=dict(arrowstyle="-|>", color=color, lw=1.4, ls=style))
    if label:
        ax.text(pts[-2][0] + lx, (pts[-2][1] + pts[-1][1]) / 2 + ly, label, fontsize=8.5, color=color, ha=ha, va="center")


ax.text(1, 130.5, "Falcon Gulf Ops Recovery — multi-agent architecture (TechX multi-agent option)", fontsize=17,
        fontweight="bold", color=INK, va="top")
ax.text(1, 127.8, "Blue: who does the stage.  Black: what it does.  Grey: how it is built.  Red: safety and human control.",
        fontsize=10, color=MUTED, va="top")
ax.text(1, 126.2, "One LangGraph thread per disruption case — a slow partner or a pending approval pauses only that case.",
        fontsize=10, color=MUTED, va="top")

# ----------------------------------------------------------------- left column: integrations, state, humans
box(1, 79, 22.5, 45.5, "API integrations", "12 mocked feeds / partner APIs", [
    "Schedule", "Movements (OOOI, airline ETA)", "ATC / flow ETA", "Turnaround tasks + ops notes", "Stands and gates",
    "Crew roster and duty clock", "Maintenance: tech log, sign-off", "Passengers (PNR, hashed ids)", "Baggage messages",
    "Seat inventory", "Hotel partner API", "Weather"], [
    "one adapter per feed (falcon/feeds):", "· source + timestamp + age on every value",
    "· timeout, retry with backoff", "· circuit breaker per feed", "· status OK / STALE / MISSING /",
    "  CONFLICT / DOWN, shown live", "· the maintenance feed is read-only"], fc="#f4f8fd", ec="#2a78d6")
box(1, 42, 22.5, 34.5, "State management", "SQLite + LangGraph checkpointer", [
    "LangGraph SqliteSaver:", "· one thread per case", "· interrupt() for approvals + waits",
    "· resume exactly where it stopped", "", "SQLite store (falcon/store.py):", "events · feed_snapshots · cases",
    "proposals · actions · approvals", "saga_steps · audit_log · messages"], [
    "", "simulated clock shared by replay,", "agents, approvals and dashboard"], fc="#f7f7f5")
box(1, 5, 22.5, 34.5, "Human control points", "never automatic", [
    "Crew Controller", "· every crew change", "Duty Ops Controller", "· aircraft swap, hold > 5 min,",
    "  retime, cancel, anything safety", "Maintenance Control", "· the ONLY source of a release;",
    "  the system reads it, never writes", "Customer Service Lead", "· mass re-accommodation > 50 pax,",
    "  hotel spend above budget"], [
    "one approval per action · timeout →", "keep current plan + escalate"], fc="#fdf3f2", ec=RED, tc=RED)

# ----------------------------------------------------------------- main flow
X, WID = 26.5, 72.5
box(X, 114, 34, 10.5, "1  Event stream", "code", ["Replayed ops day on a simulated clock;", "minute scans open one case per disruption"],
    ["falcon/replay.py · Engine.tick / _scan"])
box(X + 37, 114, 35.5, 10.5, "2  Validate inputs", "code (+ optional guard model)", [
    "Schemas, freshness, airline-vs-ATC ETA,", "prompt-injection filter on free text"],
    ["Pydantic · regex always · llama-guard3 optional"])
box(X, 100, WID, 10.5, "3  Coordinator agent", "LLM supervisor — plan checked in code with graphlib.TopologicalSorter", [
    "Plans which agents run and in what order. Code rejects cycles, unknown agents, missing checks (crew legality and",
    "maintenance status before any swap; re-accommodation only after the schedule is fixed).  Rejected → replan once → rule plan."],
    ["falcon/coordinator.py · Groq gpt-oss-120b / Llama 3.3 70B · Gemini Flash · Ollama qwen3 · or no LLM at all"])
agents = [
    ("4a  Turnaround", "LLM + tools", ["Task progress, off-block", "prediction, ground-", "resource dispatch"]),
    ("4b  Solvers", "no LLM", ["CP-SAT re-accommodation", "CP-SAT stands", "Crew legality", "Delay propagation",
                               "Maintenance check"]),
    ("4c  Passenger", "LLM + tools", ["Connections at risk,", "hold-vs-go options,", "vouchers, hotels,",
                                           "messages (EN / AR)"]),
    ("4d  Baggage", "LLM + tools", ["Bags at risk, hot", "transfers, priority", "tags, re-routing"]),
    ("4e  Crew & aircraft", "LLM + tools", ["Explains LEGAL crew", "options and SIGNED-", "OFF aircraft only;",
                                                 "otherwise escalates"]),
]
aw = (WID - 4 * 0.9) / 5
for i, (title, who, lines) in enumerate(agents):
    ax_ = X + i * (aw + 0.9)
    box(ax_, 78, aw, 18.5, title, who.replace("LLM + tools", "LLM agent + tools"), lines,
        fc="#f4f8fd" if "LLM +" in who else "#f7f7f5", size=9.2)
    arrow([(ax_ + aw / 2, 100), (ax_ + aw / 2, 96.5)])
    arrow([(ax_ + aw / 2, 78), (ax_ + aw / 2, 74.5)])
ax.text(X + WID / 2, 76.4, "run in parallel where the plan allows (LangGraph Send) · every one returns a typed Proposal: "
        "actions + rationale + data sources with timestamps + confidence", fontsize=8.5, color=MUTED, ha="center",
        bbox=dict(fc="white", ec="none", pad=1.2))
ax.text(X + WID / 2, 97.6, "the LLM only picks among code-built candidate actions and explains — it never writes a number, "
        "an id or a safety decision; invalid output → rule fallback", fontsize=8.5, color=MUTED, ha="center",
        bbox=dict(fc="white", ec="none", pad=1.2))
box(X, 64, WID, 10.5, "5  Merge and resolve", "code", [
    "One recovery plan per case. Collisions: same stand, same crew, same aircraft, same seat. Trade-offs scored with the",
    "cost model (hold vs go, swap vs wait, compress vs standard turnaround) and shown to the controller."],
    ["falcon/merge.py · costs in falcon/config.py"])
box(X + 8, 50, WID - 16, 10.5, "6  Guard", "code only — NEVER an LLM", [
    "G0 known action type · G1 crew duty/rest/sectors · G2 maintenance sign-off · G3 stands",
    "G4 seats · G5 connection times · G6 PRM / UMNR · G7 money caps · G8 data quality · G9 approvals"],
    ["falcon/guards.py · a failed rule removes the action and escalates; it cannot be approved away"],
    fc="#fdf3f2", ec=RED, lw=2.2, tc=RED)
box(X, 34.5, 31, 11.5, "7a  Safe: act automatically", "code", [
    "Ground dispatch, hot transfers, stand", "moves, single rebookings, vouchers"], ["logged and reversible"])
box(X + 35, 33.5, 37.5, 12.5, "7b  Role-based approval", "human, per action — LangGraph interrupt()", [
    "Duty Ops · Crew Controller · Maintenance ·", "Customer Service Lead. No reply in time:",
    "safe default (keep plan) + escalate"], ["falcon/approvals.py"], fc="#fdf3f2", ec=RED, tc=RED)
box(X, 20, WID, 10.5, "8  Execute as a saga — one write gateway", "code", [
    "Every action is re-checked against LIVE data and its approval record right before the write. Each step has a",
    "compensating undo; a failed step reverses the earlier steps in reverse order, then the case re-plans."],
    ["falcon/saga.py · there is no handler that can release an aircraft or create a sign-off"])
box(X, 5, 34, 11.5, "9  Wait and resume", "code", [
    "Cases sleep in the checkpointer until", "new ETA, sign-off, approval, fresh", "feed or partner API back"],
    ["interrupt() + SqliteSaver"])
box(X + 37, 5, 35.5, 11.5, "10  Log, report and demo", "code (an LLM may redraft the report)", [
    "Audit log: case → proposal → data snapshots", "→ rules → approver → undo. Delay report.",
    "Streamlit dashboard, evaluation, ROI."], ["falcon/audit.py · app.py"])

arrow([(X + 34, 119), (X + 37, 119)])
arrow([(X + 37 + 17.75, 114), (X + 37 + 17.75, 110.5)])
arrow([(X + WID / 2, 64), (X + WID / 2, 60.5)])
arrow([(X + 20, 50), (X + 20, 46)], "safe", lx=0.8)
arrow([(X + 54, 50), (X + 54, 46)], "risky or unsure", lx=0.8)
arrow([(X + 15.5, 34.5), (X + 15.5, 30.5)])
arrow([(X + 50, 33.5), (X + 50, 30.5)], "approved", lx=0.8)
arrow([(X + WID / 2, 20), (X + WID / 2, 18.2), (X + 17, 18.2), (X + 17, 16.5)])
arrow([(X + 34, 10.5), (X + 37, 10.5)])
arrow([(X + 72.5, 40), (100.7, 40), (100.7, 105), (X + WID, 105)], color=RED)
ax.text(101.7, 72, "rejected: re-plan", fontsize=9, color=RED, rotation=90, ha="center", va="center")
arrow([(X + 6, 16.5), (X + 6, 18.6), (24.9, 18.6), (24.9, 105), (X, 105)])
ax.text(25.8, 52, "next event or new data: resume", fontsize=9, color=LINE, rotation=90, ha="center", va="center")
arrow([(23.5, 119), (X, 119)], color="#2a78d6")
arrow([(23.5, 87), (X, 87)], color="#2a78d6", dashed=True)
arrow([(X, 68), (23.5, 68)], color=LINE, dashed=True)
ax.text(12.2, 77.6, "read by the agents' tools; re-read by the write gateway", fontsize=8.5, color="#2a78d6", ha="center")
ax.text(12.2, 40.4, "every stage reads and writes shared state", fontsize=8.5, color=MUTED, ha="center")
ax.text(12.2, 3.2, "approvals in, escalations out (dashboard inbox per role)", fontsize=8.5, color=RED, ha="center")

out = Path(__file__).resolve().parent
fig.savefig(out / "architecture.png", dpi=110, bbox_inches="tight", facecolor="white")
fig.savefig(out / "architecture.pdf", bbox_inches="tight", facecolor="white")
print("written:", out / "architecture.png", "and", out / "architecture.pdf")
