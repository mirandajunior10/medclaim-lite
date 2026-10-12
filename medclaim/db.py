"""Database access for the claims system.

NOTE (era-3): this module was cleaned up during the modernisation push and is
one of the few places touched by all three teams, so it stays deliberately
small. Everything else talks to SQLite through the connection handed out here.
The active database path is process-global so scripts, the API, and the tests
can each point the system at their own database without threading a handle
through every call.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "medclaim.db"
_active_db_path: str = os.environ.get("MEDCLAIM_DB", str(_DEFAULT_DB))


def set_active_db(path: str) -> None:
    """Point the system at a different SQLite file (used by seed + tests)."""
    global _active_db_path
    _active_db_path = str(path)


def active_db_path() -> str:
    return _active_db_path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_active_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    patient_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    mrn          TEXT NOT NULL,
    payer_id     TEXT NOT NULL,
    contact_email TEXT
);

CREATE TABLE IF NOT EXISTS payers (
    payer_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    payer_id       TEXT NOT NULL,
    effective_date TEXT NOT NULL,   -- ISO date, inclusive
    end_date       TEXT,            -- ISO date, inclusive; NULL = open ended
    rate_pct       INTEGER NOT NULL -- payer-allowed percentage of charges
);

-- claims.is_deleted is TRI-STATE by history (see ADR-002):
--   1     -> voided (either void path), hidden from statements
--   0     -> explicitly active
--   NULL  -> era-1 rows that predate the flag (must still be shown)
CREATE TABLE IF NOT EXISTS claims (
    id             TEXT PRIMARY KEY,
    patient_id     TEXT NOT NULL,
    payer_id       TEXT NOT NULL,
    service_date   TEXT NOT NULL,   -- ISO date
    amount_cents   INTEGER NOT NULL,
    adjustment_cents INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'submitted',
    is_deleted     INTEGER          -- nullable on purpose
);

CREATE TABLE IF NOT EXISTS remittances (
    id           TEXT PRIMARY KEY,
    claim_id     TEXT NOT NULL,
    paid_cents   INTEGER NOT NULL,
    posted_date  TEXT NOT NULL
);

-- Voids are recorded here regardless of which void path ran. Both paths also
-- flip claims.is_deleted (T-103: the legacy path used to write ONLY here).
CREATE TABLE IF NOT EXISTS void_log (
    claim_id   TEXT NOT NULL,
    reason     TEXT,
    method     TEXT NOT NULL,       -- 'legacy' | 'modern'
    voided_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
    id                     TEXT PRIMARY KEY,
    patient_id             TEXT NOT NULL,
    generated_at           TEXT NOT NULL,
    total_charges_cents    INTEGER NOT NULL,
    total_adjustments_cents INTEGER NOT NULL,
    balance_cents          INTEGER NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
