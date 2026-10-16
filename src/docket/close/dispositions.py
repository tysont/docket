"""Dispositions (TD §13.7, FR-APP-05): what was decided about each topic, read by the next run's merge.

One disposition per decided topic, keyed by the item's merge ``topic`` (its
strongest identity key: a reference, then a thread, then an agent topic),
falling back to the item id. A later item with a new id but the same
reference or thread therefore meets the disposition in merge (TD §7.3).

Decisions:

- ``done``: suppressed from then on;
- ``dismiss``: suppressed until ``until`` (a date), or for good without one;
- ``watch``: suppressed until the topic shows activity newer than the run;
- ``routed``: written to a destination by operation ``op_id``; re-surfaces on new activity;
- ``now``: handled during the session, with the recorded ``outcome``; re-surfaces on new activity.

Sources, in order: the plan's ``outcomes`` list (question, item, topic,
decision, note, until) when the plan carries one; otherwise the decide
answers whose action is an outcome; then a ``routed`` disposition for every
applied operation whose provenance names a question that has no outcome.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

DECISIONS = ("done", "routed", "watch", "dismiss", "now")
OUTCOME_ACTIONS = ("done", "watch", "dismiss", "now")


@dataclass
class Disposition:
    topic_id: str
    decision: str
    run_id: str
    item: str | None = None
    until: str | None = None
    note: str | None = None
    destination: str | None = None
    op_id: str | None = None
    outcome: str | None = None

    def row(self) -> dict[str, Any]:
        return asdict(self)


def _items(decide: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Question id -> item."""
    return {f["question_id"]: f["item"] for f in decide.get("findings") or []}


def _topic(item: dict[str, Any] | None, fallback: str | None) -> str | None:
    if item and item.get("topic"):
        return str(item["topic"])
    if item and item.get("id"):
        return str(item["id"])
    return fallback


def derive(run_id: str, decide: dict[str, Any] | None, outcomes: list[dict[str, Any]] | None,
           operations: list[dict[str, Any]], receipts: dict[str, dict[str, Any]]) -> list[Disposition]:
    """Dispositions of a closing run. ``operations`` are the approved plan's; ``receipts`` by op id."""
    decide = decide or {}
    items = _items(decide)
    by_item = {it.get("id"): it for it in items.values() if it.get("id")}
    out: dict[str, Disposition] = {}
    decided_q: set[str] = set()

    def put(d: Disposition) -> None:
        if d.topic_id and d.topic_id not in out:
            out[d.topic_id] = d

    if outcomes is not None:
        for o in outcomes:
            dec = o.get("decision")
            if dec not in DECISIONS or dec == "routed":
                continue
            qid = o.get("question")
            item = items.get(qid) if qid else None
            if item is None and o.get("item"):
                item = by_item.get(o["item"])
            topic = o.get("topic") or _topic(item, o.get("item"))
            if not topic:
                continue
            if qid:
                decided_q.add(qid)
            put(Disposition(str(topic), dec, run_id, item=(item or {}).get("id") or o.get("item"),
                            until=o.get("until"), note=o.get("note"),
                            outcome=o.get("note") if dec == "now" else None))
    else:
        for qid, ans in sorted((decide.get("answers") or {}).items()):
            dec = ans.get("action")
            if dec not in OUTCOME_ACTIONS:
                continue
            item = items.get(qid)
            topic = _topic(item, qid[2:] if qid.startswith("q_") else qid)
            ov = ans.get("overrides") or {}
            decided_q.add(qid)
            put(Disposition(str(topic), dec, run_id, item=(item or {}).get("id"), until=ov.get("until"),
                            note=ov.get("note"), outcome=ov.get("note") if dec == "now" else None))
    for op in operations:
        qid = op.get("question_id")
        rec = receipts.get(op.get("op_id", ""))
        if not qid or qid in decided_q or rec is None or rec.get("outcome") not in ("applied", "already_present"):
            continue
        item = items.get(qid) or by_item.get(op.get("item_id"))
        topic = _topic(item, op.get("item_id"))
        if not topic:
            continue
        put(Disposition(str(topic), "routed", run_id, item=(item or {}).get("id") or op.get("item_id"),
                        destination=op.get("destination"), op_id=op.get("op_id")))
    return [out[k] for k in sorted(out)]


def write(db: sqlite3.Connection, rows: list[Disposition]) -> None:
    """Record dispositions (the caller holds the transaction). The latest decision on a topic wins."""
    for d in rows:
        db.execute("INSERT OR REPLACE INTO dispositions (topic_id, decision, run_id, item, until, note, destination, "
                   "op_id, outcome) VALUES (?,?,?,?,?,?,?,?,?)",
                   (d.topic_id, d.decision, d.run_id, d.item, d.until, d.note, d.destination, d.op_id, d.outcome))
