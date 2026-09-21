"""Durable proposals, order intents, account state and exactly-once fill booking."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tayder.models import Fill, Proposal, ProposalStatus, Side, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
 decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, request_json TEXT NOT NULL,
 result_json TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS proposals (
 proposal_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, side TEXT NOT NULL,
 notional_usd REAL NOT NULL, reason TEXT NOT NULL, signal_price REAL NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT, meta_json TEXT);
CREATE TABLE IF NOT EXISTS fills (
 order_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, product_id TEXT NOT NULL,
 side TEXT NOT NULL, price REAL NOT NULL, size REAL NOT NULL, fee_usd REAL NOT NULL,
 notional_usd REAL NOT NULL, mode TEXT NOT NULL, filled_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
 payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Journal:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_state(self, key: str):
        with self._lock:
            row = self._conn.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_state(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO runtime VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value, allow_nan=False)))

    def save_proposal(self, p: Proposal) -> None:
        with self._lock:
            self._conn.execute("""INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET status=excluded.status,
                notional_usd=excluded.notional_usd, expires_at=excluded.expires_at,
                meta_json=excluded.meta_json""",
                (p.proposal_id, p.product_id, p.side.value, p.notional_usd, p.reason,
                 p.signal_price, p.status.value, p.created_at.isoformat(),
                 p.expires_at.isoformat() if p.expires_at else None,
                 json.dumps(p.meta, allow_nan=False)))

    def proposals(self) -> list[Proposal]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM proposals ORDER BY created_at").fetchall()
        return [Proposal(product_id=r["product_id"], side=Side(r["side"]),
                notional_usd=r["notional_usd"], reason=r["reason"], signal_price=r["signal_price"],
                proposal_id=r["proposal_id"], status=ProposalStatus(r["status"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                expires_at=datetime.fromisoformat(r["expires_at"]) if r["expires_at"] else None,
                meta=json.loads(r["meta_json"] or "{}")) for r in rows]

    def fills(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM fills ORDER BY filled_at")]

    def finish(self, fill: Fill, proposal: Proposal, account: dict) -> bool:
        """Atomically book a terminal fill, advance its proposal, and save cash/inventory.

        Open partial fills remain reserved until the exchange reports terminal.
        """
        with self.transaction():
            if self._conn.execute("SELECT 1 FROM fills WHERE order_id=? OR proposal_id=?",
                                  (fill.order_id, fill.proposal_id)).fetchone():
                return False
            self._conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                (fill.order_id, fill.proposal_id, fill.product_id, fill.side.value,
                 fill.price, fill.size, fill.fee_usd, fill.notional_usd, fill.mode,
                 fill.filled_at.isoformat()))
            self.save_proposal(proposal)
            self.set_state("account", account)
            self.log_event("fill", {"order_id": fill.order_id, "proposal_id": fill.proposal_id})
        return True

    def log_event(self, kind: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(ts,kind,payload_json) VALUES (?,?,?)",
                               (utcnow().isoformat(), kind, json.dumps(payload, allow_nan=False)))

    def enqueue_decision(self, proposal_id: str, request: dict) -> str:
        import hashlib
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = hashlib.sha256(encoded.encode()).hexdigest()
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?)",
                               (key, proposal_id, encoded, None, utcnow().isoformat()))
        return key

    def next_decision(self):
        with self._lock:
            row = self._conn.execute("SELECT * FROM decisions WHERE result_json IS NULL ORDER BY created_at LIMIT 1").fetchone()
            return {**dict(row), "request": json.loads(row["request_json"])} if row else None

    def finish_decision(self, key: str, result: dict) -> None:
        with self._lock:
            self._conn.execute("UPDATE decisions SET result_json=? WHERE decision_id=? AND result_json IS NULL",
                               (json.dumps(result, allow_nan=False), key))

    def decisions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM decisions ORDER BY created_at").fetchall()
            return [{"decision_id": r["decision_id"], "proposal_id": r["proposal_id"],
                     "request": json.loads(r["request_json"]),
                     "result": json.loads(r["result_json"]) if r["result_json"] else None}
                    for r in rows]

    def latest_decision(self):
        with self._lock:
            row = self._conn.execute("SELECT request_json, result_json FROM decisions WHERE result_json IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()
            return (json.loads(row[0]), json.loads(row[1])) if row else None
