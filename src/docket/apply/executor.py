"""The executor (TD §13.3).

Operations run in the fixed destination order (task store, agendas,
calendar, messages, journal), plan order within a destination. For each:

1. a receipt marked verified skips the operation;
2. ``apply(op)`` writes and returns a receipt, which is committed before
   anything else happens;
3. ``verify(op, receipt)`` reads back and the result is committed.

A crash at any point resumes from receipts: an applied but unverified
operation is applied again, which the adapter's idempotence turns into
``already_present``, and then verified. An integrity refusal (``dest.*``) is
recorded with its code and details as outcome ``refused``; an external failure
as ``failed``. Later operations that do not depend on a refused or failed one
continue; dependants are reported ``blocked``. Approval and freshness (TD
§13.1, §13.2) belong to the approve stage, not here.
"""
from __future__ import annotations

from typing import Any, Callable

from docket.apply import receipts
from docket.cli.errors import DocketError
from docket.destinations import ORDER, for_run
from docket.destinations.base import Operation, Receipt

# Test hooks for crash injection: called with the operation after the write and after the receipt commit.
AFTER_WRITE: Callable[[Operation], None] | None = None
AFTER_RECEIPT: Callable[[Operation], None] | None = None


def ordered(rc: Any, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dests = rc.ws.config.destinations

    def rank(ix_op: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        ix, op = ix_op
        cfg = dests.get(op["destination"])
        return (ORDER.get(cfg.adapter, 9) if cfg else 9, ix)

    return [op for _, op in sorted(enumerate(ops), key=rank)]


def execute(rc: Any, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run = rc.run
    db = run.store.db
    cache: dict[str, Any] = {}
    status: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    try:
        for raw in ordered(rc, ops):
            op = Operation.model_validate(raw)
            prior = receipts.existing(db, raw)
            if prior is not None and prior["verify_status"] == "verified":
                status[op.op_id] = prior["outcome"]
                rows.append({"op_id": op.op_id, "destination": op.destination, "outcome": prior["outcome"],
                             "verify_status": "verified"})
                continue
            bad = [d for d in op.depends_on if status.get(d) not in ("applied", "already_present")]
            if bad:
                status[op.op_id] = "blocked"
                rows.append({"op_id": op.op_id, "destination": op.destination, "outcome": "blocked",
                             "verify_status": "unverified", "blocked_by": bad})
                continue
            dest = cache.get(op.destination)
            if dest is None:
                dest = cache[op.destination] = for_run(rc, op.destination)
            try:
                rec = dest.apply(op)
            except DocketError as e:
                outcome = "refused" if e.kind == "integrity" else "failed"
                rec = Receipt(op_id=op.op_id, destination=op.destination, outcome=outcome, error=e.body())
            except OSError as e:
                rec = Receipt(op_id=op.op_id, destination=op.destination, outcome="failed",
                              error={"code": "dest.apply_failed", "kind": "external", "message": str(e),
                                     "retryable": True, "details": {}})
            if rec.outcome in ("refused", "failed"):
                receipts.record(run, raw, rec.outcome, None, False, error=rec.error)
                status[op.op_id] = rec.outcome
                rows.append({"op_id": op.op_id, "destination": op.destination, "outcome": rec.outcome,
                             "verify_status": "unverified", "error": rec.error})
                continue
            if AFTER_WRITE is not None:
                AFTER_WRITE(op)
            outcome = rec.outcome
            if prior is not None and prior["outcome"] == "applied" and outcome == "already_present":
                outcome = "applied"  # this plan made the change; the repeat found it in place
            receipts.record(run, raw, outcome, rec.external_id, False)
            if AFTER_RECEIPT is not None:
                AFTER_RECEIPT(op)
            vr = dest.verify(op, rec)
            receipts.record(run, raw, outcome, rec.external_id, vr.ok,
                            error=None if vr.ok else {"verify": vr.model_dump()})
            status[op.op_id] = outcome
            row = {"op_id": op.op_id, "destination": op.destination, "outcome": outcome,
                   "verify_status": "verified" if vr.ok else "unverified"}
            if not vr.ok:
                row["verify"] = vr.model_dump()
            rows.append(row)
    finally:
        for d in cache.values():
            d.close()
    return rows
