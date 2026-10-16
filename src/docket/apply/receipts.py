"""Apply receipts keyed by operation id (TD §13.3)."""
from __future__ import annotations

import sqlite3
from typing import Any

from docket.cli.errors import IntegrityError
from docket.config.digest import digest
from docket.store.runs import Run, wall_stamp

PROVENANCE = ("question_id", "item_id", "depends_on")


def payload_digest(op: dict[str, Any]) -> str:
    return digest({k: v for k, v in op.items() if k not in PROVENANCE})


def existing(conn: sqlite3.Connection, op: dict[str, Any]) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM receipts WHERE op_id=?", (op["op_id"],)).fetchone()
    if row is not None and row["payload_digest"] != payload_digest(op):
        raise IntegrityError(
            "apply.receipt_conflict",
            f"Operation {op['op_id']} already has a receipt with a different payload.",
            hint="docket plan build",
            details={"op_id": op["op_id"], "receipt_run": row["run_id"]},
        )
    return row


def record(run: Run, op: dict[str, Any], outcome: str, external_id: str | None, verified: bool,
           error: dict[str, Any] | None = None) -> dict[str, Any]:
    """Commit a receipt row and append it to the run's receipts.jsonl (with the raw error, when any)."""
    row: dict[str, Any] = {
        "op_id": op["op_id"], "destination": op["destination"], "outcome": outcome,
        "external_id": external_id, "verify_status": "verified" if verified else "unverified",
        "payload_digest": payload_digest(op),
    }
    with run.store.db:
        run.store.db.execute(
            "INSERT OR REPLACE INTO receipts (op_id, run_id, destination, outcome, external_id, verify_status, "
            "payload_digest, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (op["op_id"], run.id, op["destination"], outcome, external_id, row["verify_status"],
             row["payload_digest"], wall_stamp()),
        )
    if error is not None:
        row["error"] = error
    run.append_jsonl(run.paths.receipts, row)
    return row
