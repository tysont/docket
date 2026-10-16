"""Step receipts: one row per completed close step or hook run, keyed by ``(run, step)``.

A receipt is written in the same transaction as the step's own database
writes when it has any, so a step is either done and receipted or not done.
Outcomes ``ok``, ``warned`` and ``skipped`` are complete; ``failed`` is not and
is retried.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from docket.store.runs import wall_stamp

COMPLETE = ("ok", "warned", "skipped")


def get(db: sqlite3.Connection, run_id: str, step: str) -> dict[str, Any] | None:
    row = db.execute("SELECT outcome, json FROM step_receipts WHERE run_id=? AND step=?", (run_id, step)).fetchone()
    if row is None:
        return None
    body = json.loads(row["json"])
    body["outcome"] = row["outcome"]
    return body


def done(db: sqlite3.Connection, run_id: str, step: str) -> dict[str, Any] | None:
    """The receipt of a completed step, or None when the step must (still) run."""
    r = get(db, run_id, step)
    return r if r is not None and r["outcome"] in COMPLETE else None


def put(db: sqlite3.Connection, run_id: str, step: str, outcome: str, body: dict[str, Any]) -> None:
    """Write a receipt. The caller holds the transaction when the step has other writes."""
    db.execute("INSERT OR REPLACE INTO step_receipts (run_id, step, outcome, json, recorded_at) VALUES (?,?,?,?,?)",
               (run_id, step, outcome, json.dumps(body, sort_keys=True, ensure_ascii=False), wall_stamp()))


def for_run(db: sqlite3.Connection, run_id: str, prefix: str = "") -> list[dict[str, Any]]:
    out = []
    for row in db.execute("SELECT step, outcome, json, recorded_at FROM step_receipts WHERE run_id=? AND step LIKE ? "
                          "ORDER BY step", (run_id, prefix + "%")):
        body = json.loads(row["json"])
        out.append({"step": row["step"], "outcome": row["outcome"], "recorded_at": row["recorded_at"], **body})
    return out
