# CLAUDE.md — standing rules for Falcon Gulf Ops Recovery

Read this file, `BRIEF.md` and `docs/ARCHITECTURE.md` at the start of every session.
`BRIEF.md` = WHAT we build (the brief, verbatim). `docs/ARCHITECTURE.md` = the agent design in detail.
This file = HOW we build it. If they ever disagree, the safety rules here win.

## Who you're working with
- Owner: Irfan (GitHub: AwaisNevrekar). Wants working, demo-ready results, not long explanations.
- Explain in short, simple language. When something is done, say what changed and how to check it.
- Windows laptop, PowerShell. Give every command in PowerShell form, one block at a time.

## Environment (Windows)
- Python virtual env lives in `.venv`. Always run Python as `.\.venv\Scripts\python.exe` — never bare
  `python` or `streamlit` (they may not be on PATH).
- Run the app: `.\.venv\Scripts\python.exe -m streamlit run app.py` (opens http://localhost:8501).
- Read and write every text file with `encoding="utf-8"` (Windows defaults to cp1252 and breaks Arabic).
- Create `.env` files with `Set-Content -Encoding ascii`, never `echo > .env`.
- API keys live only in `.env` (git-ignored). Never print a key, never commit it, never put it in code.
- The whole system must run with NO API key (rule-based fallbacks for every LLM step) and offline.

## Stack
Python 3.10+ · LangGraph (supervisor + agent subgraphs, `Send` for parallel workers, `interrupt()` for
approvals, SQLite checkpointer for durable wait/resume) · Pydantic · SQLite · **OR-Tools CP-SAT** (re-accommodation,
stand allocation) with PuLP as fallback · Streamlit · optional Langfuse. LLMs via one OpenAI-compatible client:
Groq (gpt-oss-120b / Llama 3.3 70B), Gemini Flash, or Ollama (qwen3:8b / qwen3:4b, llama-guard3).

## Architecture — TechX MULTI-AGENT option (docs/reference/TechX_Agentic_Workflow_MultiAgent.pdf)
This MUST be a multi-agent system. One LangGraph run per disruption case; each case has its own thread, so a
slow partner or a pending approval never blocks other flights.

1. **Event stream (code)** — replayed ops-day feeds arrive on a simulated clock; scheduled scans every minute.
2. **Validate inputs (code + guard LLM)** — Pydantic schemas, freshness (stale > limit), cross-feed consistency,
   prompt-injection filter on free-text fields (llama-guard3 / Prompt Guard if available, regex always).
3. **Coordinator agent (LLM)** — reads the case and plans which agents run, in what order, with dependencies.
   **Code checks the plan with `graphlib.TopologicalSorter`** (no cycles, only known agents, required
   dependencies present, e.g. crew check before any swap). Rejected plan → replan once → rule plan.
4. **Specialist agents, in parallel where the plan allows:**
   - 4a **Turnaround agent (LLM + tools)** — task progress, delay prediction, ground-resource dispatch.
   - 4b **Solvers (no LLM)** — re-accommodation (CP-SAT), stand/gate allocation (CP-SAT), crew legality
     calculator, delay-propagation calculator. Deterministic, explainable, reproducible.
   - 4c **Passenger agent (LLM + tools)** — connections at risk, vouchers/hotels, passenger messages.
   - 4d **Baggage agent (LLM + tools)** — bag connection risk, hot-transfer/priority actions.
   - 4e **Crew & aircraft agent (LLM + tools)** — duty-clock awareness, swap options, maintenance status.
   Every agent returns a typed `Proposal` (actions + rationale + data sources with timestamps + confidence).
5. **Merge and resolve (code)** — combine proposals into one recovery plan in shared state; detect collisions
   (same stand, same crew, same seat, same aircraft); score trade-offs with the cost model in `config.py`.
6. **Guard (code only, NEVER an LLM)** — hard limits re-run on the merged plan. A failed hard rule removes the
   action and escalates; it is never "approved away".
7. **Approval routing** — 7a safe → act automatically (logged, reversible). 7b role-based sign-off:
   Duty Ops Controller, Crew Controller, Maintenance Control, Customer Service Lead. Timeout → safe default
   (keep current plan) + escalate. Approvals are per action, never one blanket approval.
8. **Execute as a saga (code)** — every step has a compensating undo; a failed step reverses earlier steps in
   reverse order. All writes go through one write gateway that re-checks live data first.
9. **Wait and resume (code)** — cases sleep in the checkpointer until the next event (new ETA, sign-off,
   approval, partner API back) and resume where they stopped.
10. **Log, report and demo** — every agent call, tool call, data snapshot, rule result, approver and undo goes
    to the audit log; an LLM may draft the controller's delay report from the log (rule template if no key).

## Safety rules (non-negotiable — enforce in code, test every one)
- **Crew:** never propose, rank or display an assignment that breaks a duty/rest limit. Limits live in
  `config.py` (demo values, configurable — not a legal statement). Every crew change needs Crew Controller approval.
- **Aircraft:** never release, dispatch or swap in an aircraft without a current maintenance sign-off record.
  The system reads sign-off; it can never create one. No sign-off → aircraft is unavailable, escalate.
- **Schedule:** network-wide changes (cancel, swap, retime beyond threshold, hold beyond threshold) are only
  proposals until a Duty Ops Controller approves them.
- **LLMs only plan and explain.** They never decide safety, never write numbers into actions, and every LLM
  output is checked: unknown flight/crew/aircraft IDs or values not present in feeds → rejected → rule fallback.
- **Data:** stale, missing or conflicting feeds are shown as such (never silently filled). Actions that depend
  on unreliable data are held or escalated, never executed on a guess.
- **Partners:** every external call has a timeout, retry with backoff and a circuit breaker; failures affect only
  the cases that need that partner.
- **Fairness:** re-accommodation follows the published priority table in ARCHITECTURE.md §6 — same inputs, same
  result. PNR groups stay together unless split is the only option (then escalate).
- **Privacy:** passenger names and booking refs never reach an LLM or the logs; use hashed IDs.
- **Audit:** every executed action links to: the case, the proposal, the data snapshot IDs + timestamps used,
  the rules that passed, the approver (or "auto" + rule), and its undo.

## Project layout (keep to this)
```
app.py                 Streamlit ops-control dashboard (all screens)
falcon/
  config.py            every threshold, limit and cost figure
  feeds/               one adapter per feed: freshness stamp, timeout, retry, circuit breaker
  replay.py            simulated clock + replayed ops day (seeded, deterministic)
  coordinator.py       coordinator agent + graphlib plan check
  agents/              turnaround.py, passenger.py, baggage.py, crew_aircraft.py
  solvers/             reaccommodation.py, stands.py, crew_legality.py, propagation.py
  merge.py             merge, collision detection, trade-off scoring
  guards.py            hard rules (code only)
  approvals.py         role-based routing, timeouts, safe defaults
  saga.py              saga executor with compensating actions + write gateway
  workflow.py          LangGraph graph (stages 1–10)
  llm.py               OpenAI-compatible client + fallback
  audit.py             audit log + case replay queries
  baseline.py          manual-operations baseline policy for the evaluation
  evaluation.py        runs baseline vs system on the same day → docs/EVALUATION.md
  business_case.py     ROI model with editable assumptions
data/                  generator + generated mock day (clearly fictional)
scenarios.py           scripted end-to-end scenarios → outputs/test_log.md + .csv
tests/                 unit tests (guards, solvers, plan check, saga undo, feed adapters)
docs/                  ARCHITECTURE.md, architecture diagram (PDF/PNG), EVALUATION.md, BUSINESS_CASE.md
```

## How to work
- Build in the phases of ARCHITECTURE.md §10. Before each phase, say in 3–5 lines what you'll do.
- After each phase: run tests, run the app once, commit. Don't stop at "should work" — run it.
- Every bug fix gets a test that would have caught it. Every safety rule gets a test that tries to break it.
- Keep the replayed day deterministic (fixed seed) so baseline vs system comparisons are fair and repeatable.
- Done means: `.\.venv\Scripts\python.exe scenarios.py` passes, `.\.venv\Scripts\python.exe -m falcon.evaluation`
  writes docs/EVALUATION.md, and the app starts without errors.
- Keep README.md current: what it does, how to run it, a 5-minute demo script, the safety-rules table.

## Git & GitHub
- Remote: `https://github.com/AwaisNevrekar/falcon-ops-recovery`.
- Commit after each working phase with a clear message. Push at the end of each session or when asked.
- Check `git status` before committing: `.env`, `.venv/`, `*.db` and `__pycache__/` must never be staged.

## Deliverables checklist (from the brief)
- [ ] Working end-to-end system, demonstrated live on a replayed operations day
- [ ] Architecture diagram: agent design, API integrations, state management, human control points
- [ ] Evaluation report: delay minutes, missed connections, rebooking time — system vs baseline
- [ ] Business-case model: delay costs avoided, compensation reduced, staff hours saved (airline + ground handler)
