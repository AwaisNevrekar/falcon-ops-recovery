"""SQLite state store: the tables of ARCHITECTURE.md section 4. Times are simulated minutes."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, t INTEGER, event_id TEXT, kind TEXT, title TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS feed_snapshots(snapshot_id TEXT PRIMARY KEY, feed TEXT, key TEXT, fetched_at INTEGER,
    age_s INTEGER, status TEXT, source TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, type TEXT, flight TEXT, title TEXT, status TEXT,
    opened_at INTEGER, closed_at INTEGER, iteration INTEGER DEFAULT 0, wait TEXT, summary TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, case_id TEXT, iteration INTEGER, agent TEXT, t INTEGER,
    rationale TEXT, confidence REAL, llm_used INTEGER, data_refs TEXT, warnings TEXT, assumptions TEXT, extras TEXT);
CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY, case_id TEXT, iteration INTEGER, agent TEXT, type TEXT,
    category TEXT, target TEXT, params TEXT, why TEXT, bundle TEXT, requires TEXT, inputs TEXT, signature TEXT,
    status TEXT, decided_by TEXT, rules TEXT, undo TEXT, est_cost REAL, est_delay REAL, t INTEGER, executed_at INTEGER);
CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY, case_id TEXT, action_id TEXT, signature TEXT,
    role TEXT, summary TEXT, detail TEXT, requested_at INTEGER, deadline INTEGER, status TEXT, decided_at INTEGER,
    decided_by TEXT, note TEXT, escalated_from TEXT);
CREATE TABLE IF NOT EXISTS saga_steps(id INTEGER PRIMARY KEY, case_id TEXT, bundle TEXT, action_id TEXT, step TEXT,
    seq INTEGER, status TEXT, t INTEGER, detail TEXT, undo TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, t INTEGER, case_id TEXT, stage TEXT, kind TEXT,
    actor TEXT, action_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, t INTEGER, case_id TEXT, pax TEXT, lang TEXT,
    template TEXT, body TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, t INTEGER, mode TEXT, name TEXT, value REAL);
CREATE INDEX IF NOT EXISTS ix_audit_case ON audit_log(case_id);
CREATE INDEX IF NOT EXISTS ix_actions_case ON actions(case_id);
"""


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple, set)):
        return json.dumps(list(v) if isinstance(v, (set, tuple)) else v, ensure_ascii=False, default=str)
    if isinstance(v, bool):
        return int(v)
    return v


class Store:
    """Thin thread-safe wrapper (LangGraph runs parallel workers in threads)."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript("PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF;" + SCHEMA)

    def add(self, table: str, replace: bool = False, **row: Any) -> int:
        cols = ", ".join(row)
        sql = f"INSERT {'OR REPLACE' if replace else 'OR IGNORE'} INTO {table}({cols}) VALUES ({', '.join('?' * len(row))})"
        with self.lock:
            return self.conn.execute(sql, [_enc(v) for v in row.values()]).lastrowid

    def set(self, table: str, key_col: str, key: Any, **row: Any) -> None:
        sql = f"UPDATE {table} SET {', '.join(c + '=?' for c in row)} WHERE {key_col}=?"
        with self.lock:
            self.conn.execute(sql, [_enc(v) for v in row.values()] + [key])

    def q(self, sql: str, *args: Any) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args: Any) -> dict | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
