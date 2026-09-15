"""SQLite journal for proposals, decisions, fills."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tayder.models import Fill, Proposal


SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    side TEXT NOT NULL,
    notional_usd REAL NOT NULL,
    reason TEXT NOT NULL,
    signal_price REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    order_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee_usd REAL NOT NULL,
    notional_usd REAL NOT NULL,
    mode TEXT NOT NULL,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class Journal:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save_proposal(self, p: Proposal) -> None:
        self._conn.execute(
            """
            INSERT INTO proposals (
                proposal_id, product_id, side, notional_usd, reason,
                signal_price, status, created_at, expires_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                status=excluded.status,
                expires_at=excluded.expires_at,
                meta_json=excluded.meta_json
            """,
            (
                p.proposal_id,
                p.product_id,
                p.side.value,
                p.notional_usd,
                p.reason,
                p.signal_price,
                p.status.value,
                p.created_at.isoformat(),
                p.expires_at.isoformat() if p.expires_at else None,
                json.dumps(p.meta),
            ),
        )
        self._conn.commit()

    def save_fill(self, f: Fill) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO fills (
                order_id, proposal_id, product_id, side, price, size,
                fee_usd, notional_usd, mode, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f.order_id,
                f.proposal_id,
                f.product_id,
                f.side.value,
                f.price,
                f.size,
                f.fee_usd,
                f.notional_usd,
                f.mode,
                f.filled_at.isoformat(),
            ),
        )
        self._conn.commit()

    def log_event(self, kind: str, payload: dict) -> None:
        from tayder.models import utcnow

        self._conn.execute(
            "INSERT INTO events (ts, kind, payload_json) VALUES (?, ?, ?)",
            (utcnow().isoformat(), kind, json.dumps(payload)),
        )
        self._conn.commit()
