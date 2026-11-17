# Project brief — Falcon Gulf Ops Recovery

## Basics
- **Project name:** Falcon Gulf Ops Recovery (multi-agent turnaround & disruption recovery)
- **GitHub repo:** https://github.com/AwaisNevrekar/falcon-ops-recovery
- **Python package name:** `falcon`
- **Architecture:** TechX **multi-agent** option (`docs/reference/TechX_Agentic_Workflow_MultiAgent.pdf`)
- **Detailed design:** `docs/ARCHITECTURE.md` — the agents, feeds, rules, scenarios, evaluation and ROI model.
- **Users:** operations controllers (main user) and role-based approvers. Passengers only receive messages.
- **Message languages:** English and Arabic (Hindi optional).

## The brief (verbatim)

**Aircraft Turnaround & Disruption Recovery — Aviation & Airport Operations**
Keep a hub airline on schedule by coordinating turnarounds and recovering from disruption in minutes, not hours.

**THE BRIEF**
Falcon Gulf Airways operates 420 daily departures from its hub, each relying on a tightly timed turnaround of
fuelling, catering, cleaning, baggage, and crew changes. One late inbound flight cascades across the network, and on
disrupted days controllers rebook up to 3,000 passengers by hand while baggage misses connections and crew approach
their duty limits.

Build an autonomous system that improves one or more parts of turnaround coordination, disruption recovery, or
passenger re-accommodation, designing your own agent architecture and tool workflow.

**IDEAS (POTENTIAL FOCUS AREAS)**
- Turnaround task orchestration & delay prediction
- Passenger rebooking & re-accommodation
- Ground-handling resource dispatch
- Crew duty-limit awareness
- Baggage connection risk detection
- Hotel & meal voucher automation
- Stand & gate reallocation
- Operations-control copilot & delay reporting

**YOUR DEMO**
Show a realistic disrupted operations day running from live operational feeds to executed recovery actions. Your
system must integrate multiple data sources/APIs, show how it balances competing priorities (such as on-time departure
against protecting passenger connections), surface stale or missing data, log the rationale for every action, and
escalate crew, aircraft, and safety-related decisions to a human controller.

**SAFETY RULES (GUARDRAILS & BOUNDARIES)**
The system must never assign crew beyond legal duty limits, release an aircraft without maintenance sign-off, or
finalise network-wide schedule changes without human approval. Explain how you handle:
- Boundaries between autonomous ground-task coordination and human-approved crew, aircraft, or schedule changes
- Stale, delayed, or conflicting operational feeds during fast-moving disruption
- Fair, rule-based re-accommodation that protects priority passengers (reduced mobility, unaccompanied travellers,
  tight connections)
- API timeouts or partner outages without stalling unrelated flights
- A complete audit trail linking every recovery action to its data and approvals

**EXPECTED DELIVERABLES**
- A working end-to-end system demonstrated live on a replayed operations day.
- An architecture diagram detailing your agent design, API integrations, state management, and human control points.
- An evaluation report comparing delay minutes, missed connections, or rebooking time against baseline operations.
- A business-case model quantifying delay costs avoided, passenger compensation reduced, and staff hours saved.

**GOOD PROJECTS WILL SHOW**
- Creative, well-engineered agent architecture that coordinates multiple live operational data streams (30%).
- Measurable reduction in delay propagation, missed connections, or recovery time compared with baseline (25%).
- Clear visibility into trade-offs, data freshness, and human controller escalation (20%).
- A compelling commercial ROI model for the airline and its ground-handling partners (25%).

## Who uses it
- **Duty Ops Controller (main screen):** sees the network, disruptions, recommended recovery plans and trade-offs;
  approves schedule changes (holds, swaps, cancellations).
- **Crew Controller:** approves every crew change. Can never be offered an illegal option.
- **Maintenance Control:** the only source of aircraft release. The system reads sign-off; it never gives it.
- **Customer Service Lead:** approves mass re-accommodation above the auto limit and voucher overrides.
- **Ground handling partner:** receives task dispatches automatically.

## Data feeds (all mocked, replayed from `data/`) — see ARCHITECTURE.md §3
Schedule · flight movements (OOOI/ETA) · turnaround task progress · stands/gates · crew roster & duty clock ·
maintenance status & release sign-off · passengers & connections (PNR) · baggage messages · seat inventory ·
hotel partner API · weather. Every feed can be late, stale, missing, contradictory, or time out.

## Human control points (never automatic)
- Any crew change → Crew Controller
- Any aircraft swap or release question → Maintenance Control sign-off must already exist; swap → Duty Ops Controller
- Any schedule change visible to the network (delay > threshold, hold, cancel, swap, retime) → Duty Ops Controller
- Mass re-accommodation above `AUTO_REBOOK_MAX_PAX` per flight → Customer Service Lead
- Anything safety-related → Duty Ops Controller immediately
- Approval timeout → safe default (keep current plan, do not change crew/aircraft/schedule) + escalate

## Autonomous (no approval, still logged and reversible)
Ground-task resequencing and dispatch · baggage hot-transfer / priority tags · individual same-day rebooking that
passes every rule · meal vouchers within policy · stand reassignment with no passenger-gate impact · staff alerts.

## Must-show replay scenarios (≥ 12) — full list in ARCHITECTURE.md §8
Late inbound cascade · hold-vs-go trade-off for connections · mass rebooking with PRM / unaccompanied minor / tight
connections · crew near duty limit (escalate, never illegal) · aircraft tech fault without sign-off · stand conflict ·
baggage misconnect risk · stale movement feed · conflicting ETA between feeds · hotel API outage (other flights
continue) · approval timeout → safe default · saga failure → undo · prompt-injection in a free-text feed.

## Value / ROI inputs (editable assumptions in `falcon/business_case.py`)
420 departures/day · up to 3,000 rebooked passengers on a disrupted day · delay cost per minute · compensation and
care cost per passenger · hotel and meal cost · controller and agent hourly cost · ground-handler SLA penalties.

---

## First message to paste into Claude Code

```
Read CLAUDE.md, BRIEF.md, docs/ARCHITECTURE.md and the PDFs in docs/reference.
This must be a MULTI-AGENT system following the TechX multi-agent option.
1. Summarise in 10–15 lines what you'll build, and list the build phases from ARCHITECTURE.md §10.
2. Set up the project: create .venv, requirements.txt, .gitignore, .env.example, and connect this
   folder to the GitHub repo in BRIEF.md (git init, add remote, first commit, push).
3. Build phase by phase. After each phase: run the tests, commit, and tell me in 3 lines what works.
4. At the end: run the full replayed day in baseline and system modes, generate the evaluation report,
   start the app, and tell me how to demo it in 5 minutes.
```
