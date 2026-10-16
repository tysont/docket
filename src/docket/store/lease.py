"""Run leases (TD §5.5).

A lease row is ``(run_id, owner, pid, host, acquired_at, heartbeat_at)``. The
owner is a session token: ``$DOCKET_SESSION`` when the caller sets one, else a
token Docket writes to the run directory on first use, so separate shell calls
from one agent share it without environment variables. An owner is dead when
its pid is gone on this host or its heartbeat is older than the timeout. A dead
lease is adopted and the adoption reported; a live foreign lease refuses with
``lease.held``.
"""
from __future__ import annotations

import os
import secrets
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from docket.cli.errors import LeaseError

DEFAULT_TIMEOUT = timedelta(minutes=15)
SESSION_FILE = "session"


def wall_now() -> datetime:
    return datetime.now(timezone.utc)


def session_token(run_dir: Path, env: Mapping[str, str], *, create: bool) -> str | None:
    if env.get("DOCKET_SESSION"):
        return env["DOCKET_SESSION"]
    p = run_dir / SESSION_FILE
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    if not create:
        return None
    tok = "s_" + secrets.token_hex(8)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok + "\n", encoding="utf-8")
    return tok


def owner_pid() -> int:
    """The pid that stands for this session: the calling process (an agent or a shell)."""
    return os.getppid()


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_dead(row: sqlite3.Row | dict, *, now: datetime, timeout: timedelta = DEFAULT_TIMEOUT) -> bool:
    hb = datetime.fromisoformat(row["heartbeat_at"])
    if now - hb > timeout:
        return True
    return row["host"] == socket.gethostname() and not pid_alive(row["pid"])


def get(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leases WHERE run_id=?", (run_id,)).fetchone()


def acquire(conn: sqlite3.Connection, run_id: str, token: str, *, steal_cmd: list[str],
            timeout: timedelta = DEFAULT_TIMEOUT, force: bool = False) -> dict[str, Any]:
    """Acquire or renew the lease inside the caller's transaction. Returns a report."""
    now = wall_now()
    row = get(conn, run_id)
    report: dict[str, Any] = {"owner": token, "adopted_from": None, "stolen_from": None}
    if row is not None and row["owner"] != token:
        if force:
            report["stolen_from"] = row["owner"]
        elif is_dead(row, now=now, timeout=timeout):
            report["adopted_from"] = row["owner"]
        else:
            raise LeaseError(
                "lease.held",
                f"Run {run_id} is held by session {row['owner']} (pid {row['pid']} on {row['host']}, "
                f"heartbeat {row['heartbeat_at']}).",
                hint=" ".join(steal_cmd),
                details={"run": run_id, "owner": row["owner"], "pid": row["pid"], "host": row["host"],
                         "heartbeat_at": row["heartbeat_at"], "steal": steal_cmd},
                next_command=steal_cmd,
            )
    stamp = now.isoformat()
    if row is None or row["owner"] != token:
        conn.execute(
            "INSERT OR REPLACE INTO leases (run_id, owner, pid, host, acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
            (run_id, token, owner_pid(), socket.gethostname(), stamp, stamp),
        )
    else:
        conn.execute("UPDATE leases SET heartbeat_at=?, pid=? WHERE run_id=?", (stamp, owner_pid(), run_id))
    conn.execute("UPDATE runs SET lease_owner=? WHERE run_id=?", (token, run_id))
    return report


def heartbeat(conn: sqlite3.Connection, run_id: str, token: str | None) -> bool:
    """Renew the heartbeat when ``token`` owns the lease. The only write ``docket next`` makes."""
    if not token:
        return False
    row = get(conn, run_id)
    if row is None or row["owner"] != token:
        return False
    with conn:
        conn.execute("UPDATE leases SET heartbeat_at=? WHERE run_id=?", (wall_now().isoformat(), run_id))
    return True


def foreign_live(conn: sqlite3.Connection, run_id: str, token: str | None,
                 timeout: timedelta = DEFAULT_TIMEOUT) -> sqlite3.Row | None:
    row = get(conn, run_id)
    if row is None or row["owner"] == token:
        return None
    return None if is_dead(row, now=wall_now(), timeout=timeout) else row
