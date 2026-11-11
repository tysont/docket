"""The workspace database (TD §4.2): SQLite in WAL mode, one schema with a version.

A new database is created at ``SCHEMA_VERSION``. A database at another version is
refused: one written by a newer Docket with ``store.db_too_new``, an older one with
``state.too_old`` (its state must be migrated before this Docket can use it).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from docket.cli.errors import StateError

SCHEMA_VERSION = 15

SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, graph TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
  sandbox INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE leases (
  run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER, host TEXT,
  acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
);
CREATE TABLE cursors (
  source TEXT NOT NULL, unit TEXT NOT NULL, position TEXT, coverage_end TEXT, updated_run TEXT,
  PRIMARY KEY (source, unit)
);
CREATE TABLE cursor_proposals (
  run_id TEXT NOT NULL, source TEXT NOT NULL, unit TEXT NOT NULL, position TEXT, coverage_end TEXT,
  model TEXT, complete INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (run_id, source, unit)
);
CREATE TABLE units (
  source TEXT NOT NULL, unit_id TEXT NOT NULL, name TEXT, kind TEXT, type TEXT, class TEXT, tier TEXT,
  first_seen TEXT, last_seen TEXT, gone_at TEXT, classified_by TEXT, members INTEGER, rule TEXT,
  PRIMARY KEY (source, unit_id)
);
CREATE TABLE processed_ids (
  source TEXT NOT NULL, record_id TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY (source, record_id)
);
CREATE TABLE task_results (
  input_digest TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT,
  result TEXT NOT NULL, accepted_at TEXT NOT NULL
);
CREATE TABLE task_labels (
  task_id TEXT PRIMARY KEY, text_digest TEXT NOT NULL, label TEXT NOT NULL, run_id TEXT
);
CREATE TABLE receipts (
  op_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, destination TEXT NOT NULL, outcome TEXT NOT NULL,
  external_id TEXT, verify_status TEXT, payload_digest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE step_receipts (
  run_id TEXT NOT NULL, step TEXT NOT NULL, outcome TEXT NOT NULL, json TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY (run_id, step)
);
CREATE TABLE dispositions (
  topic_id TEXT PRIMARY KEY, decision TEXT NOT NULL, run_id TEXT, item TEXT, until TEXT, note TEXT,
  destination TEXT, op_id TEXT, outcome TEXT
);
CREATE TABLE item_ledger (
  key TEXT PRIMARY KEY, topic TEXT NOT NULL UNIQUE, source TEXT, title TEXT, url TEXT,
  state TEXT NOT NULL, state_at TEXT, first_run TEXT, first_at TEXT, last_run TEXT,
  due TEXT, est INTEGER, area TEXT, placements TEXT NOT NULL DEFAULT '[]', history TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX item_ledger_state ON item_ledger (state);
CREATE TABLE ledgers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ledger TEXT NOT NULL, run_id TEXT, key TEXT, json TEXT NOT NULL
);
CREATE INDEX ledgers_by_name ON ledgers (ledger, run_id);
CREATE TABLE unexpected (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, command TEXT, area TEXT NOT NULL,
  detail TEXT NOT NULL, sample TEXT, reading TEXT NOT NULL, extend TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX unexpected_shape ON unexpected (area, detail);
CREATE TABLE unexpected_accepted (
  area TEXT NOT NULL, detail TEXT NOT NULL, reason TEXT NOT NULL, accepted_at TEXT NOT NULL,
  PRIMARY KEY (area, detail)
)
"""


def version(conn: sqlite3.Connection) -> int | None:
    """The schema version of an open database; None when it has no tables yet."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is None:
        return None
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    if not row:
        return 0
    v = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    return int(v[0]) if v else 0


def open_db(path: Path) -> sqlite3.Connection:
    """Open the database, creating it when it does not exist. Refuses any other schema version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    current = version(conn)
    if current is None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        try:
            for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))
        except Exception:
            conn.execute("ROLLBACK")
            conn.close()
            raise
        conn.execute("COMMIT")
    elif current > SCHEMA_VERSION:
        conn.close()
        raise StateError(
            "store.db_too_new",
            f"The database at {path} is schema version {current}; this Docket understands {SCHEMA_VERSION}.",
            hint="Upgrade Docket to a version that supports database schema " + str(current),
            details={"db_version": current, "supported": SCHEMA_VERSION, "path": str(path)},
        )
    elif current < SCHEMA_VERSION:
        conn.close()
        raise StateError(
            "state.too_old",
            f"The database at {path} is schema version {current}, older than this Docket's {SCHEMA_VERSION}. "
            f"The state must be migrated before this Docket can use it.",
            hint="Migrate the workspace state to schema " + str(SCHEMA_VERSION) + ", or start from a new state "
                 "directory",
            details={"db_version": current, "supported": SCHEMA_VERSION, "path": str(path)},
        )
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
