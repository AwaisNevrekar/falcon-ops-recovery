"""Falcon Gulf Ops Recovery — operations-control dashboard (Streamlit).

Run:  .\\.venv\\Scripts\\python.exe -m streamlit run app.py      (opens http://localhost:8501)

Screens: Ops overview · Cases · Approvals (inbox per role) · Feeds · Audit (+ Evaluation and Business case).
Everything shown here is read from the same engine, store and audit log the agents use. All data is fictional.
"""
from __future__ import annotations

import os
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from falcon import config as C  # noqa: E402
from falcon.approvals import SimulatedApprovers  # noqa: E402
from falcon.guards import RULES  # noqa: E402
from falcon.llm import LLM  # noqa: E402
from falcon.models import signoff_valid  # noqa: E402
from falcon.store import loads  # noqa: E402
from falcon.workflow import Engine  # noqa: E402

ROOT = Path(__file__).resolve().parent
DAY = pd.Timestamp("2026-11-17")
st.set_page_config(page_title="Falcon Gulf Ops Recovery", page_icon="✈️", layout="wide")


# --------------------------------------------------------------------------- engine (one per replay run)
@st.cache_resource(show_spinner="Generating the replayed operations day ...")
def get_engine(run_id: int) -> Engine:
    path = str(Path(os.environ.get("FALCON_DB_DIR", ROOT)) / f"falcon_state_{run_id}.db")
    for f in (path, path + ".ckpt.db"):
        try:
            os.remove(f)
        except OSError:
            pass
    return Engine(db_path=path)


@st.cache_resource
def real_llm() -> LLM:
    return LLM()


ss = st.session_state
ss.setdefault("run_id", 0)
ss.setdefault("who", "irfan")
eng = get_engine(ss.run_id)
world, store = eng.world, eng.store

try:
    DARK = st.context.theme.type == "dark"
except Exception:
    DARK = False
INK = "#c3c2b7" if DARK else "#52514e"
GRID = "#2c2c2a" if DARK else "#e1e0d9"
BLUE, ORANGE = ("#3987e5", "#d95926") if DARK else ("#2a78d6", "#eb6834")
# status palette: reserved for state, always paired with a shape and a label (never colour alone)
STATUS = {"On time": ("#0ca30c", "circle"), "Late up to 15 min": ("#fab219", "diamond"),
          "Late over 15 min": ("#ec835a", "square"), "Cancelled or cannot depart": ("#d03b3b", "x")}
BADGE = {"OK": "🟢 OK", "DEGRADED": "🟡 DEGRADED", "STALE": "🟠 STALE", "MISSING": "🟠 MISSING", "CONFLICT": "🟠 CONFLICT",
         "DOWN": "🔴 DOWN", "CLOSED": "✅ closed", "WAITING": "⏸️ waiting", "WAITING_APPROVAL": "🧑‍✈️ needs approval",
         "RUNNING": "▶️ running", "ERROR": "🔴 error"}
INBOXES = [C.DUTY_OPS, C.CREW_CTRL, C.MAINT, C.CS_LEAD]


def ts(minute):
    return DAY + pd.Timedelta(minutes=int(minute))


def table(rows, cols=None, height=None):
    df = pd.DataFrame(rows)
    if cols and not df.empty:
        df = df[[c for c in cols if c in df.columns]]
    st.dataframe(df, hide_index=True, width="stretch", height=height or "auto")


def pending_rows(role=None):
    return [r for r in eng.approvals.inbox(role) if r["status"] == "PENDING"]


def advance(minutes: int):
    eng.tick(minutes)


def run_to_next_approval(limit: int = 90):
    for _ in range(limit):
        eng.tick()
        if pending_rows():
            break


# --------------------------------------------------------------------------- sidebar: replay controls
with st.sidebar:
    st.title("✈️ Falcon Gulf Ops Recovery")
    st.caption("Multi-agent turnaround and disruption recovery · replayed operations day · all data fictional")
    st.metric("Simulated time", C.hm(world.now), border=True)
    # callbacks run BEFORE the page is redrawn, so the clock and every table always show the new state
    c1, c2, c3 = st.columns(3)
    c1.button("+1 min", width="stretch", on_click=advance, args=(1,))
    c2.button("+5 min", width="stretch", on_click=advance, args=(5,))
    c3.button("+15 min", width="stretch", on_click=advance, args=(15,))
    nxt = next((e for e in world.events if e["t"] > world.now), None)
    st.button(f"▶ Next event: {nxt['id']} at {C.hm(nxt['t'])}" if nxt else "No more scripted events", width="stretch",
              disabled=nxt is None, type="primary", on_click=eng.run_until, args=(nxt["t"] if nxt else world.now,))
    st.button("▶ Run until a human is needed", width="stretch", on_click=run_to_next_approval)
    auto = st.toggle("Auto-play", key="autoplay", help="Advances the clock by itself and pauses when an approval waits.")
    speed = st.select_slider("Speed (simulated minutes per second)", [1, 2, 5, 10], value=2, disabled=not auto)
    st.divider()
    ss.who = st.text_input("Your name (recorded on approvals)", ss.who)
    sim_on = st.toggle("Simulated approvers answer for me", value=eng.sim is not None,
                       help="Off: you approve in the Approvals tab. On: each role answers after its response time.")
    if sim_on and eng.sim is None:
        eng.sim = SimulatedApprovers(eng.approvals, world.events)
    elif not sim_on:
        eng.sim = None
    key_llm = real_llm()
    use_llm = st.toggle("Use the LLM to plan and explain", value=bool(getattr(eng.llm, "client", None)),
                        disabled=key_llm.client is None,
                        help="Off (or no API key): every agent uses its rule-based fallback. Safety never depends on the LLM.")
    wanted = key_llm if use_llm and key_llm.client is not None else None
    if wanted is not None and eng.llm is not wanted:
        wanted.secret_check = eng.llm.secret_check
        eng.llm = wanted
    elif wanted is None and getattr(eng.llm, "client", None) is not None:
        off = LLM.off()
        off.secret_check = eng.llm.secret_check
        eng.llm = off
    d = eng.llm.describe()
    st.caption(f"LLM: **{d['provider']}** {d['model'] or ''} · {d['mode']} · {d['calls']} calls")
    if st.button("↺ Restart the day at 13:45", width="stretch"):
        ss.run_id += 1
        ss.autoplay = False
        st.rerun()
    st.caption(f"Seed {world.seed} · same seed, same day, every time")

stats = eng.stats()
metrics = world.metrics()
n_pending = len(pending_rows())
if n_pending:
    st.warning(f"🧑‍✈️ {n_pending} approval request(s) waiting for a human — open the **Approvals** tab. "
               + " · ".join(f"{C.ROLE_NAMES[r]}: {len(pending_rows(r))}" for r in INBOXES if pending_rows(r)))
# tab labels stay constant on purpose: a label that changes would reset the selected tab on every click
tabs = st.tabs(["🛫 Ops overview", "🗂️ Cases", "🧑‍✈️ Approvals", "📡 Feeds", "🧾 Audit", "📊 Evaluation",
                "💰 Business case"], key="main_tabs")

# =========================================================================== ops overview
with tabs[0]:
    k = st.columns(6)
    k[0].metric("Open cases", stats["cases_open"], f"{stats['cases']} opened today", delta_color="off", delta_arrow="off", border=True)
    k[1].metric("Waiting on a human", n_pending, "approval requests", delta_color="off", delta_arrow="off", border=True)
    k[2].metric("Hub on-time (within 15 min)", f"{metrics['hub_otp15_pct']}%", f"{metrics['flights_delayed_15']} flights late",
                delta_color="off", delta_arrow="off", border=True)
    k[3].metric("Passengers re-accommodated", metrics["reaccommodated_pax"], f"of {metrics['disrupted_pax']} disrupted",
                delta_color="off", delta_arrow="off", border=True)
    k[4].metric("Actions executed", stats["actions_executed"],
                f"{stats['actions_auto']} automatic · {stats['actions_approved']} approved", delta_color="off", delta_arrow="off", border=True)
    k[5].metric("Held or blocked by the guard", stats["actions_held_bad_data"] + stats["actions_blocked_by_guard"],
                f"{stats['escalations']} escalations", delta_color="off", delta_arrow="off", border=True)

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Hub departures: delay against scheduled time")
        lo, hi = world.now - 90, world.now + 240
        fig = go.Figure()
        deps = [x for x in world.legs.values() if x["kind"] == "DEP" and lo <= x["std"] <= hi]
        for name, (color, symbol) in STATUS.items():
            pts = []
            for x in deps:
                delay = x["etd"] - x["std"]
                state = ("Cancelled or cannot depart" if x["cancelled"] or x["blocked"] else "On time" if delay <= 0
                         else "Late up to 15 min" if delay <= 15 else "Late over 15 min")
                if state == name:
                    pts.append((x, 0 if x["cancelled"] else delay))
            fig.add_trace(go.Scatter(
                x=[ts(x["std"]) for x, _ in pts], y=[dl for _, dl in pts], mode="markers", name=f"{name} ({len(pts)})",
                marker=dict(color=color, symbol=symbol, size=10, line=dict(color="#1a1a19" if DARK else "#fcfcfb", width=2)),
                customdata=[[x["fid"], x["dest"], C.hm(x["std"]), "cancelled" if x["cancelled"] else C.hm(x["etd"]), x["ac"],
                             x["stand"], x["hold"], ", ".join(m.replace("_", " ") for m in x["measures"]) or "none",
                             x["blocked"] or "", "departed" if x["atd"] is not None else "not departed"] for x, _ in pts],
                hovertemplate="<b>%{customdata[0]}</b> to %{customdata[1]}<br>scheduled %{customdata[2]} · estimated "
                              "%{customdata[3]} (%{customdata[9]})<br>aircraft %{customdata[4]} · stand %{customdata[5]}"
                              "<br>hold %{customdata[6]} min · ground measures: %{customdata[7]} %{customdata[8]}<extra></extra>"))
        fig.add_vline(x=ts(world.now), line_width=1, line_dash="dot", line_color=INK)
        fig.add_annotation(x=ts(world.now), y=1.02, yref="paper", text=f"now {C.hm(world.now)}", showarrow=False,
                           font=dict(color=INK, size=12))
        fig.update_layout(height=400, margin=dict(l=60, r=10, t=30, b=10), paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)", font=dict(color=INK),
                          legend=dict(orientation="h", y=-0.3, x=0), hoverlabel=dict(align="left"),
                          yaxis=dict(title="minutes late", gridcolor=GRID, zerolinecolor=GRID, rangemode="tozero",
                                     automargin=True, title_standoff=12),
                          xaxis=dict(title="scheduled departure", gridcolor=GRID, tickformat="%H:%M", automargin=True))
        st.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False})
        with st.expander("Table view of the flights that need attention"):
            table([{"flight": x["fid"], "to": x["dest"], "std": C.hm(x["std"]), "etd": C.hm(x["etd"]),
                    "late (min)": x["etd"] - x["std"], "hold (min)": x["hold"], "cannot depart": x["blocked"] or "",
                    "cancelled": x["cancelled"], "aircraft": x["ac"], "stand": x["stand"]}
                   for x in sorted(deps, key=lambda x: x["std"]) if x["etd"] > x["std"] or x["cancelled"] or x["blocked"]],
                  height=260)
    with right:
        st.subheader("What has happened")
        table([{"time": C.hm(t), "#": eid, "event": title, "what it shows": shows}
               for t, eid, title, shows in reversed(world.event_log)], height=200)
        st.subheader("Latest executed actions")
        table([{"time": C.hm(r["executed_at"]), "case": r["case_id"], "action": r["type"], "target": r["target"],
                "decided by": r["decided_by"]}
               for r in store.q("SELECT * FROM actions WHERE status='EXECUTED' ORDER BY executed_at DESC, rowid DESC LIMIT 12")],
              height=220)

# =========================================================================== cases
with tabs[1]:
    cases = sorted(eng.cases.values(), key=lambda c: (c["status"] == "CLOSED", -c["opened_at"]))
    if not cases:
        st.info("No disruption yet. Advance the clock: the sandstorm arrives at 14:05.")
    else:
        fa, fb = st.columns([1, 3])
        show = fa.multiselect("Show", ["needs approval", "waiting", "closed"], default=["needs approval", "waiting", "closed"])
        want = {"needs approval": "WAITING_APPROVAL", "waiting": "WAITING", "closed": "CLOSED"}
        visible = [c for c in cases if c["status"] in {want[s] for s in show} or c["status"] in ("RUNNING", "ERROR")]
        if visible:
            cid = fb.selectbox("Case", [c["case_id"] for c in visible], key="case_pick",
                               format_func=lambda i: f"{BADGE.get(eng.cases[i]['status'], '')}  {i} · {eng.cases[i]['title'][:110]}")
            case = eng.cases[cid]
            st.markdown(f"### {cid} · {case['title']}")
            st.caption(f"{case['type']} · opened {C.hm(case['opened_at'])} · status {BADGE.get(case['status'])} · "
                       f"planning rounds {case.get('iteration', 0)}"
                       + (f" · waiting for: {', '.join(w.get('ref') or w.get('key') or w.get('id', '') for w in case['waits'])}"
                          if case.get("waits") else ""))
            a_plan, a_trade = st.columns([2, 3])
            with a_plan:
                st.markdown("**Stage 2 · inputs checked**")
                val = store.one("SELECT detail FROM audit_log WHERE case_id=? AND kind='VALIDATED' ORDER BY id DESC", cid)
                v = loads(val["detail"], {}) if val else {}
                eta = v.get("eta")
                if eta:
                    st.write(f"ETA used **{C.hm(eta['eta'])}** — {eta['basis']} (airline {C.hm(eta['airline_eta'])}, "
                             f"ATC {C.hm(eta['atc_eta'])}; status **{eta['status']}**)")
                if v.get("blocked_notes"):
                    st.error(f"Free-text ops note blocked as a suspected prompt injection "
                             f"({', '.join(v['blocked_notes']['patterns'])}). No agent saw it.")
                for f in v.get("feeds_not_ok", []):
                    st.warning(f"Feed **{f['feed']}** is {f['status']} {f['flagged']}")
                st.markdown("**Stage 3 · coordinator plan (checked in code with graphlib)**")
                plans = [loads(r["detail"], {}) for r in
                         store.q("SELECT detail FROM audit_log WHERE case_id=? AND kind='PLAN' ORDER BY id", cid)]
                p = plans[-1] if plans else {}
                for i, pl in enumerate(plans, 1):          # "‖" = agents that run in parallel, "→" = then
                    st.write(f"Round {i}: " + "  →  ".join(" ‖ ".join(f"`{a}`" for a in wave) for wave in pl.get("waves", [])))
                checks = [loads(r["detail"]) for r in store.q(
                    "SELECT detail FROM audit_log WHERE case_id=? AND kind='PLAN_CHECK' ORDER BY id DESC LIMIT 3", cid)]
                st.caption(f"Plan source: **{p.get('source', '?')}** — {p.get('reason', '')}  ·  checks: "
                           + "; ".join(f"{c['source']} {'passed' if c['passed'] else 'REJECTED: ' + c['error']}"
                                       for c in reversed(checks)))
            with a_trade:
                st.markdown("**Stage 5 · trade-offs (cost model in `config.py`)**")
                merged = store.q("SELECT detail FROM audit_log WHERE case_id=? AND kind='MERGED' ORDER BY id", cid)
                trades = [t for m in merged for t in loads(m["detail"], {}).get("tradeoffs", [])]
                if not trades:
                    st.caption("No competing options in this case.")
                for t in trades[:6]:
                    with st.container(border=True):
                        st.write(f"**{t['decision']}**  \n_{t['priority_conflict']}_")
                        table(t["options"])
                        st.caption(f"Recommended: **{t['recommended']}** · decided by: {t['needs']}")
            st.markdown("**Stage 4 · what each agent and solver proposed**")
            props = store.q("SELECT * FROM proposals WHERE case_id=? ORDER BY id", cid)
            last = max((r["iteration"] for r in props), default=1)
            for r in props:
                label = (f"round {r['iteration']} · {r['agent']} · confidence {r['confidence']:.2f}"
                         + (" · LLM" if r["llm_used"] else " · rules") + f" — {r['rationale'][:120]}")
                with st.expander(label, expanded=False):
                    st.write(r["rationale"])
                    for wmsg in loads(r["warnings"], []):
                        st.warning(wmsg)
                    refs = loads(r["data_refs"], [])
                    if refs:
                        st.caption("Data used (every value has a source and an age)")
                        table([{"feed": x["feed"], "record": x["record_id"], "read at": C.hm(x["fetched_at"]),
                                "age (s)": x["age_s"], "status": x["status"]} for x in refs], height=180)
            st.markdown("**Stages 6–8 · the plan: guard result, approval and execution of every action**")
            rows = []
            for a in store.q("SELECT * FROM actions WHERE case_id=? ORDER BY iteration, t, action_id", cid):
                rules = loads(a["rules"], [])
                failed = [f"{x['rule']}: {x['detail']}" for x in rules if not x["passed"]]
                rows.append({"round": a["iteration"], "action": a["type"], "target": a["target"], "status": a["status"],
                             "decided by": a["decided_by"] or "", "why": a["why"],
                             "rules passed": " ".join(sorted({x["rule"] for x in rules if x["passed"]})),
                             "rule failed": "; ".join(failed), "category": a["category"] or ""})
            table(rows, height=300)
            st.markdown("**Stage 10 · delay report for the controller (drafted from the audit log)**")
            if st.button("Draft the report with the LLM", disabled=not eng.llm.enabled, key="llm_report"):
                st.info(eng.audit.report(cid, eng.llm))
            st.code(eng.audit.report(cid), language=None)

# =========================================================================== approvals
with tabs[2]:
    st.caption("One approval per action — never a blanket approval. No answer before the deadline: the current plan is kept "
               "(safe default) and the request is escalated. Illegal crew options and unsigned aircraft never appear here.")
    st.markdown("Waiting now: " + " · ".join(f"**{C.ROLE_NAMES[r]} {len(pending_rows(r))}**" if pending_rows(r)
                                             else f"{C.ROLE_NAMES[r]} 0" for r in INBOXES))
    role_tabs = st.tabs([C.ROLE_NAMES[r] for r in INBOXES] + ["Escalations (duty managers)"], key="role_tabs")
    for i, role in enumerate(INBOXES + ["ESC"]):
        with role_tabs[i]:
            roles = [role] if role != "ESC" else sorted(set(C.ESCALATION_ROLE.values()))
            rows = [r for rr in roles for r in eng.approvals.inbox(rr)]
            todo = [r for r in rows if r["status"] == "PENDING"]
            if not todo:
                st.success("Nothing is waiting for a decision.")
            for r in todo:
                det = loads(r["detail"], {})
                with st.container(border=True):
                    left_min = r["deadline"] - world.now
                    st.markdown(f"**{r['summary']}**")
                    st.caption(f"Case {r['case_id']} · {det.get('category', '')} · asked {C.hm(r['requested_at'])} · "
                               f"answer needed by **{C.hm(r['deadline'])}** ({left_min} min left)"
                               + (f" · escalated from {r['escalated_from'].split('|')[-1]}" if r["escalated_from"] else ""))
                    with st.expander("Details and data behind this request"):
                        st.json(det.get("params", {}), expanded=False)
                        tr = eng.audit.trace(r["action_id"])
                        if tr:
                            st.write(f"Proposed by **{tr['proposal']['agent']}**: {tr['proposal']['rationale']}")
                            table([{"feed": x["feed"], "record": x["record_id"], "read at": x["fetched"], "age (s)": x["age_s"],
                                    "status": x["status"]} for x in tr["data"]], height=150)
                            st.caption("Guard: " + " · ".join(f"{x['rule']} {'✓' if x['passed'] else '✗'}" for x in tr["rules"]))
                    note = st.text_input("Note (optional)", key="note" + r["approval_id"], label_visibility="collapsed",
                                         placeholder="Note (optional)")
                    b1, b2, _ = st.columns([1, 1, 4])
                    if b1.button("✅ Approve", key="ok" + r["approval_id"], type="primary", width="stretch"):
                        eng.decide(r["approval_id"], "APPROVED", f"{ss.who or 'controller'}", note)
                        st.rerun()
                    if b2.button("✖ Reject", key="no" + r["approval_id"], width="stretch"):
                        eng.decide(r["approval_id"], "REJECTED", f"{ss.who or 'controller'}", note)
                        st.rerun()
            notices = [r for r in rows if r["status"] == "NOTICE"]
            if notices:
                st.markdown("**Escalations and notices**")
            for r in notices:
                n1, n2 = st.columns([8, 1])
                n1.warning(f"{C.hm(r['requested_at'])} · {r['case_id']} · {r['summary']}")
                if n2.button("Seen", key="ack" + r["approval_id"]):
                    eng.approvals.acknowledge(r["approval_id"], ss.who or "controller")
                    st.rerun()
            if role == C.MAINT:
                st.markdown("**Maintenance release** — only Maintenance Control can record one. The system reads it; it can "
                            "never create it.")
                unsigned = [reg for reg, a in sorted(world.aircraft.items()) if not signoff_valid(a, world.now)]
                if not unsigned:
                    st.caption("Every aircraft has a current release sign-off.")
                for reg in unsigned:
                    m1, m2 = st.columns([5, 2])
                    m1.error(f"{reg}: no current sign-off · open items: {'; '.join(world.aircraft[reg]['open_items']) or 'none'}")
                    if m2.button(f"Record release for {reg}", key="mx" + reg):
                        eng.maintenance_signoff(reg, f"MX-{(ss.who or 'controller').upper()}")
                        st.rerun()
            done = [r for rr in roles for r in eng.approvals.inbox(rr, include_done=True)
                    if r["status"] in ("APPROVED", "REJECTED", "TIMEOUT")]
            if done:
                with st.expander(f"Decided ({len(done)})"):
                    table([{"asked": C.hm(r["requested_at"]), "decided": C.hm(r["decided_at"]), "result": r["status"],
                            "by": r["decided_by"] or "(no reply)", "case": r["case_id"], "request": r["summary"],
                            "note": r["note"] or ""} for r in done], height=220)

# =========================================================================== feeds
with tabs[3]:
    st.caption("Every value used in a decision carries its source, the time it was read and its age. Stale, missing, "
               "conflicting or unavailable data is shown here and holds the actions that depend on it.")
    health = eng.hub.health()
    table([{"feed": h["feed"], "status": BADGE.get(h["status"], h["status"]), "age (s)": h["age_s"],
            "stale after (s)": str(h["stale_after_s"] or "-"), "circuit breaker": h["breaker"], "calls": h["calls"],
            "retries": h["retries"], "failed calls": h["failures"], "records flagged": h["records_flagged"],
            "note": h["note"], "source": h["source"]} for h in health], height=460)
    st.subheader("Simulate a feed problem")
    f1, f2, f3, f4, f5 = st.columns([2, 2, 2, 2, 2])
    feed = f1.selectbox("Feed", [h["feed"] for h in health], index=10)
    kind = f2.selectbox("Problem", ["down", "slow", "stale", "missing"])
    mins = f3.number_input("For (simulated min)", 5, 120, 20)
    key = f4.text_input("Only this record (flight / aircraft), optional")
    f5.write("")
    if f5.button("Inject", type="primary", width="stretch"):
        try:
            world.add_fault(feed, kind, until=world.now + int(mins), key=key.strip() or None,
                            **({"since": world.now - 15} if kind == "stale" else {}))
            st.success(f"{feed} is now {kind} for {mins} simulated minutes. Advance the clock and watch the cases.")
        except Exception as ex:
            st.error(f"Could not inject: {ex}")
    active = [{"feed": f["feed"], "record": f["key"] or "(whole feed)", "problem": f["kind"], "since": C.hm(f["since"]),
               "until": C.hm(f["until"])} for f in world.faults]
    if active:
        st.markdown("**Active problems**")
        table(active)

# =========================================================================== audit
with tabs[4]:
    st.caption("Every agent call, plan check, guard result, approval, saga step and undo. Passenger names and booking "
               "references are never written: only hashed ids exist.")
    q1, q2, q3 = st.columns([2, 2, 3])
    case_ids = ["(all)"] + sorted(eng.cases)
    pick = q1.selectbox("Case", case_ids, key="audit_case")
    kinds = ["(all)"] + [r["kind"] for r in store.q("SELECT DISTINCT kind FROM audit_log ORDER BY kind")]
    kind_pick = q2.selectbox("Entry type", kinds)
    text = q3.text_input("Search (flight, aircraft, rule, name ...)")
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    if pick != "(all)":
        sql, args = sql + " AND case_id=?", args + [pick]
    if kind_pick != "(all)":
        sql, args = sql + " AND kind=?", args + [kind_pick]
    if text:
        sql, args = sql + " AND (detail LIKE ? OR action_id LIKE ? OR actor LIKE ?)", args + [f"%{text}%"] * 3
    log = store.q(sql + " ORDER BY id DESC LIMIT 400", *args)
    table([{"time": C.hm(r["t"]), "case": r["case_id"], "stage": r["stage"], "entry": r["kind"], "by": r["actor"],
            "action": r["action_id"], "detail": r["detail"]} for r in log], height=300)
    if pick != "(all)":
        st.subheader(f"Replay {pick} step by step")
        tl = eng.audit.timeline(pick)
        step = st.slider("Step", 1, max(1, len(tl)), max(1, len(tl)), key="replay_step") if len(tl) > 1 else 1
        table([{"#": i + 1, "time": r["time"], "stage": r["stage"], "entry": r["kind"], "by": r["actor"]}
               for i, r in enumerate(tl[:step])], height=220)
        if tl:
            st.json(tl[step - 1]["detail"], expanded=2)
        acts = store.q("SELECT action_id, type, target FROM actions WHERE case_id=? AND status='EXECUTED' ORDER BY t", pick)
        if acts:
            st.subheader("Trace one executed action: data → rules → approver → undo")
            aid = st.selectbox("Action", [a["action_id"] for a in acts],
                               format_func=lambda i: next(f"{a['type']} {a['target']} ({i})" for a in acts if a["action_id"] == i))
            tr = eng.audit.trace(aid)
            t1, t2 = st.columns(2)
            with t1:
                st.markdown(f"**Action** `{tr['action']['type']}` on `{tr['action']['target']}` — {tr['action']['why']}")
                st.markdown(f"**Proposed by** {tr['proposal']['agent']} "
                            f"({'LLM' if tr['proposal']['llm_used'] else 'rules'}): {tr['proposal']['rationale']}")
                st.markdown("**Data snapshots used**")
                table([{"feed": x["feed"], "record": x["record_id"], "read at": x["fetched"], "age (s)": x["age_s"],
                        "status": x["status"], "snapshot": x["snapshot_id"]} for x in tr["data"]], height=180)
            with t2:
                st.markdown("**Rules checked right before the write**")
                table([{"rule": x["rule"], "passed": "✓" if x["passed"] else "✗", "meaning": RULES.get(x["rule"], ""),
                        "detail": x["detail"]} for x in tr["rules"]], height=180)
                st.markdown(f"**Approver** {tr['approver']['by']}"
                            + (f" ({C.ROLE_NAMES.get(tr['approver']['role'], '')}, {tr['approver']['at']})"
                               if tr["approver"].get("role") else f" — {tr['approver'].get('rule', '')}"))
                st.markdown(f"**Undo** `{tr['undo']}`")
                table([{"step": x["step"], "result": x["status"], "time": C.hm(x["t"])} for x in tr["saga"]], height=140)

# =========================================================================== evaluation / business case (phase 6)
with tabs[5]:
    st.info("The evaluation report (baseline vs system on the same replayed day) appears here after "
            "`python -m falcon.evaluation` has been run.")
with tabs[6]:
    st.info("The editable business-case model appears here once the evaluation has been run.")

# --------------------------------------------------------------------------- auto-play
if ss.get("autoplay"):
    if pending_rows() and eng.sim is None:
        st.toast("Paused: an approval is waiting for you in the Approvals tab.")
    elif world.now < C.REPLAY_END:
        time.sleep(1.0)
        eng.tick(int(speed))
        st.rerun()
