"""The workspace database (TD §4.2): SQLite in WAL mode, forward-only numbered migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from docket.cli.errors import StateError

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0, sandbox INTEGER NOT NULL DEFAULT 0, lease_owner TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE cursors (
  source TEXT NOT NULL, unit TEXT NOT NULL, position TEXT, coverage_end TEXT, updated_run TEXT,
  PRIMARY KEY (source, unit)
);
CREATE TABLE cursor_proposals (
  run_id TEXT NOT NULL, source TEXT NOT NULL, unit TEXT NOT NULL, position TEXT, coverage_end TEXT,
  PRIMARY KEY (run_id, source, unit)
);
CREATE TABLE units (
  source TEXT NOT NULL, unit_id TEXT NOT NULL, name TEXT, kind TEXT, class TEXT, tier TEXT,
  first_seen TEXT, last_seen TEXT, classified_by TEXT, PRIMARY KEY (source, unit_id)
);
CREATE TABLE processed_ids (
  source TEXT NOT NULL, record_id TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY (source, record_id)
);
CREATE TABLE dispositions (
  topic_id TEXT PRIMARY KEY, decision TEXT NOT NULL, run_id TEXT, item TEXT, until TEXT, note TEXT
);
CREATE TABLE receipts (
  op_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, destination TEXT NOT NULL, outcome TEXT NOT NULL,
  external_id TEXT, verify_status TEXT, payload_digest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE unexpected (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, command TEXT, area TEXT NOT NULL,
  detail TEXT NOT NULL, sample TEXT, reading TEXT NOT NULL, extend TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX unexpected_shape ON unexpected (area, detail);
CREATE TABLE unexpected_accepted (
  area TEXT NOT NULL, detail TEXT NOT NULL, reason TEXT NOT NULL, accepted_at TEXT NOT NULL,
  PRIMARY KEY (area, detail)
);
CREATE TABLE ledgers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ledger TEXT NOT NULL, run_id TEXT, key TEXT, json TEXT NOT NULL
);
CREATE TABLE leases (
  run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER, host TEXT,
  acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
);
"""),
    # M2 collection: proposals carry their generation, cursor model and completeness;
    # the unit registry keeps the membership count classification read.
    (2, """
ALTER TABLE cursor_proposals ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cursor_proposals ADD COLUMN model TEXT;
ALTER TABLE cursor_proposals ADD COLUMN complete INTEGER NOT NULL DEFAULT 1;
ALTER TABLE units ADD COLUMN members INTEGER;
ALTER TABLE units ADD COLUMN rule TEXT;
"""),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    if not row:
        return 0
    v = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    return int(v[0]) if v else 0


def open_db(path: Path) -> sqlite3.Connection:
    """Open (creating and migrating) the database. Refuses a database newer than this code."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    current = _version(conn)
    if current > SCHEMA_VERSION:
        conn.close()
        raise StateError(
            "store.db_too_new",
            f"The database at {path} is schema version {current}; this Docket understands up to {SCHEMA_VERSION}.",
            hint="Upgrade Docket to a version that supports database schema " + str(current),
            details={"db_version": current, "supported": SCHEMA_VERSION, "path": str(path)},
        )
    if current < SCHEMA_VERSION:
        conn.execute("PRAGMA journal_mode=WAL")
        for num, sql in MIGRATIONS:
            if num <= current:
                continue
            conn.execute("BEGIN")
            try:
                _apply(conn, num, sql)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply(conn: sqlite3.Connection, num: int, sql: str) -> None:
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        conn.execute(stmt)
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)", (str(num),))
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)", (f"migration.{num}", "applied"))
