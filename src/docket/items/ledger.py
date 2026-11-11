"""The item ledger (TD §13.8): the master record of every item Docket has put in front of
the operator.

One row per item, keyed by a short stable key derived from the item's topic. A row carries
the item's state, every decision made on it, and its placements: the task line, calendar
block or agenda entry it became. A state is where an item is; a decision is what moves it.
Destinations are projections of the ledger: what Docket writes into them carries the key,
so an item is recognised wherever it lands and whatever tool reads it next.

The ledger lives in Docket's own state. No destination is its home, so the same ledger
serves a Markdown task list today and another task store tomorrow.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from typing import Any

STATES = ("new", "backlog", "scheduled", "raised", "monitoring", "acknowledged", "done", "ignored")
OPEN_STATES = ("new", "backlog", "scheduled", "raised", "monitoring")

# A decision that wrote to a destination ("routed") takes its state from what was written to.
_ROUTED = {"markdown_tasks": "backlog", "markdown_agenda": "raised", "calendar_exec": "scheduled",
           "calendar_file": "scheduled", "calendar_mcp": "scheduled"}
_DECIDED = {"watch": "monitoring", "ack": "acknowledged", "now": "done", "done": "done", "dismiss": "ignored"}


def item_key(topic: str) -> str:
    """A short stable key for an item: ``i`` and eight base32 characters of its topic's digest."""
    d = hashlib.sha256(str(topic).encode("utf-8")).digest()
    return "i" + base64.b32encode(d).decode("ascii").lower()[:8]


def state_of(decision: str | None, adapter: str | None = None) -> str | None:
    """The state a decision leaves an item in; None when the decision does not move it."""
    if decision == "routed":
        return _ROUTED.get(adapter or "", "backlog" if adapter is None else None)
    return _DECIDED.get(decision or "")


def _load(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["placements"] = json.loads(out.get("placements") or "[]")
    out["history"] = json.loads(out.get("history") or "[]")
    return out


def rows(db: sqlite3.Connection, state: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM item_ledger" + (" WHERE state=?" if state else "") + " ORDER BY first_at, key"
    return [_load(r) for r in db.execute(sql, (state,) if state else ()).fetchall()]


def get(db: sqlite3.Connection, key_or_topic: str) -> dict[str, Any] | None:
    r = db.execute("SELECT * FROM item_ledger WHERE key=? OR topic=?", (key_or_topic, key_or_topic)).fetchone()
    return _load(r) if r is not None else None


def derive(run_id: str, at: str, *, kept: list[dict[str, Any]], decided: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a closing run says about its items.

    ``kept`` are the items the run put forward (``id``, ``topic``, ``title``, ``url``,
    ``source``). ``decided`` are the decisions made, by topic: ``decision`` and, when it
    wrote to a destination, ``destination``, ``adapter``, ``op_id`` and where (``path``,
    ``anchor``, ``key``); also ``due``, ``est``, ``area`` and ``note`` when known.
    """
    by_topic = {str(d["topic"]): d for d in decided if d.get("topic")}
    out = []
    for it in kept:
        topic = str(it.get("topic") or it.get("id") or "")
        if not topic:
            continue
        d = by_topic.get(topic)
        row: dict[str, Any] = {"key": item_key(topic), "topic": topic, "source": it.get("source"),
                               "title": it.get("title"), "url": it.get("url"), "run": run_id, "at": at}
        if d is not None:
            row["decision"] = d.get("decision")
            row["state"] = state_of(d.get("decision"), d.get("adapter"))
            row["note"] = d.get("note")
            for k in ("due", "est", "area"):
                if d.get(k) not in (None, ""):
                    row[k] = d[k]
            if d.get("destination"):
                row["placement"] = {k: d[k] for k in ("destination", "adapter", "op_id", "path", "anchor", "key")
                                    if d.get(k) not in (None, "")}
                row["placement"]["run"] = run_id
        out.append(row)
    return out


def write(db: sqlite3.Connection, derived: list[dict[str, Any]]) -> None:
    """Fold a run's rows into the ledger (the caller holds the transaction). A row the
    ledger has keeps its first sighting; a decision appends to its history and moves its
    state; an item seen again and not decided only has its last run updated."""
    for d in derived:
        cur = db.execute("SELECT * FROM item_ledger WHERE key=?", (d["key"],)).fetchone()
        old = _load(cur) if cur is not None else None
        history = list(old["history"]) if old else []
        placements = list(old["placements"]) if old else []
        state = old["state"] if old else "new"
        state_at = old["state_at"] if old else d["at"]
        if d.get("decision"):
            new_state = d.get("state") or state
            history.append({k: v for k, v in (("run", d["run"]), ("at", d["at"]), ("decision", d["decision"]),
                                              ("state", new_state), ("note", d.get("note"))) if v is not None})
            if new_state != state:
                state, state_at = new_state, d["at"]
        if d.get("placement") and d["placement"] not in placements:
            placements.append(d["placement"])
        db.execute(
            "INSERT OR REPLACE INTO item_ledger (key, topic, source, title, url, state, state_at, first_run, first_at, "
            "last_run, due, est, area, placements, history) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["key"], d["topic"], d.get("source") or (old or {}).get("source"),
             d.get("title") or (old or {}).get("title"), d.get("url") or (old or {}).get("url"), state, state_at,
             old["first_run"] if old else d["run"], old["first_at"] if old else d["at"], d["run"],
             d.get("due") or (old or {}).get("due"), d.get("est") if d.get("est") is not None else (old or {}).get("est"),
             d.get("area") or (old or {}).get("area"),
             json.dumps(placements, sort_keys=True), json.dumps(history, sort_keys=True)))


def import_rows(db: sqlite3.Connection, items: list[Any], *, run_id: str, at: str) -> dict[str, Any]:
    """Enter items by hand (the caller holds the transaction): each needs a ``title``; ``url``,
    ``state`` (default ``backlog``), ``due``, ``est``, ``area``, ``source`` and ``note`` are
    kept when given. ``key`` is the key already written where the item lives, and
    ``destination`` with ``adapter`` names that place, so the ledger follows the item there.
    A row the ledger already has is skipped, never overwritten."""
    from docket.text.durations import parse_duration
    imported, skipped, problems = 0, 0, []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            problems.append({"row": i, "problem": "needs a title"})
            continue
        state = raw.get("state") or "backlog"
        if state not in STATES:
            problems.append({"row": i, "problem": f"unknown state {state!r}; states are {', '.join(STATES)}"})
            continue
        title = str(raw["title"]).strip()
        topic = "import:" + str(raw.get("url") or title)
        key = item_key(topic)
        if raw.get("key") not in (None, ""):      # the key already written where the item lives
            key = str(raw["key"])
            if not re.fullmatch(r"i[a-z2-7]{8}", key):
                problems.append({"row": i, "problem": f"key {key!r} is not an item key (i and eight characters)"})
                continue
            topic = "import:" + key
        placements: list[dict[str, Any]] = []
        if raw.get("destination") or raw.get("adapter"):
            if not (raw.get("destination") and raw.get("adapter")):
                problems.append({"row": i, "problem": "a place needs both destination and adapter"})
                continue
            placements = [{"destination": str(raw["destination"]), "adapter": str(raw["adapter"]), "run": run_id}]
        if db.execute("SELECT 1 FROM item_ledger WHERE key=?", (key,)).fetchone() is not None:
            skipped += 1
            continue
        est = None
        if raw.get("est") not in (None, ""):
            try:
                est = parse_duration(raw["est"])
            except Exception:
                problems.append({"row": i, "problem": f"est {raw['est']!r} is not a duration"})
                continue
        history = [{k: v for k, v in (("run", run_id), ("at", at), ("decision", "import"), ("state", state),
                                      ("note", raw.get("note"))) if v is not None}]
        db.execute(
            "INSERT INTO item_ledger (key, topic, source, title, url, state, state_at, first_run, first_at, last_run, "
            "due, est, area, placements, history) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, topic, raw.get("source") or "import", title, raw.get("url"), state, at, run_id, at, run_id,
             raw.get("due"), est, raw.get("area"), json.dumps(placements, sort_keys=True),
             json.dumps(history, sort_keys=True)))
        imported += 1
    return {"imported": imported, "skipped": skipped, "problems": problems}


def observe(db: sqlite3.Connection, seen: dict[str, dict[str, str] | None], *, run_id: str, at: str,
            write: bool = True) -> list[dict[str, Any]]:
    """Follow what happened in the destinations (the caller holds the transaction).

    ``seen`` maps a destination to the status of the keys it holds (``open`` or ``done``),
    or to None when it could not be read. An item whose state came from a placement there
    moves to ``done`` when its line is closed, or gone after having been seen there. A
    destination that is absent from ``seen``, unreadable, or that never wrote the key moves
    nothing. Returns the moves; with ``write`` false nothing
    is written.
    """
    moves = []
    for row in rows(db):
        if row["state"] not in ("backlog", "raised", "scheduled") or not row["placements"]:
            continue
        # a scheduled item still lives on its task line: the block is where the work is, the line is the item
        want = "raised" if row["state"] == "raised" else "backlog"
        placed = [p for p in row["placements"] if state_of("routed", p.get("adapter")) == want]
        if not placed:
            continue
        dest = placed[-1].get("destination")
        status = seen.get(dest) if dest else None
        if status is None:
            continue
        found = status.get(row["key"])
        if found == "open":
            if not placed[-1].get("seen") and write:
                placed[-1]["seen"] = True      # the key is on a line there: its absence will mean something
                db.execute("UPDATE item_ledger SET placements=? WHERE key=?",
                           (json.dumps(row["placements"], sort_keys=True), row["key"]))
            continue
        if found is None and not placed[-1].get("seen"):
            continue  # a destination that never wrote the key says nothing by not holding it
        note = f"closed in {dest}" if found == "done" else f"no longer in {dest}"
        moves.append({"key": row["key"], "title": row["title"], "was": row["state"], "state": "done", "note": note})
        if write:
            history = row["history"] + [{"run": run_id, "at": at, "decision": "observed", "state": "done", "note": note}]
            db.execute("UPDATE item_ledger SET state='done', state_at=?, history=? WHERE key=?",
                       (at, json.dumps(history, sort_keys=True), row["key"]))
    return moves


def read_back(ws: Any, *, run: Any = None, env: Any = None) -> dict[str, dict[str, str] | None]:
    """What every destination that can report it says about the keys it holds. A destination
    reports through ``key_status``; one without it is left out, and one that fails reads None."""
    from docket.destinations import build, env_for
    out: dict[str, dict[str, str] | None] = {}
    for did in sorted(ws.config.destinations):
        dest = None
        try:
            dest = build(did, ws.config.destinations[did], env_for(ws, run=run, env=env))
            if not hasattr(dest, "key_status"):
                continue
            out[did] = dest.key_status()
        except Exception:  # a destination that cannot be read is not evidence of anything
            out[did] = None
        finally:
            if dest is not None:
                try:
                    dest.close()
                except Exception:
                    pass
    return out


def place_blocks(db: sqlite3.Connection, blocks: list[dict[str, Any]], *, destination: str, adapter: str,
                 run_id: str, at: str) -> list[dict[str, Any]]:
    """Track a plan's standing blocks back to their items (the caller holds the transaction).

    ``blocks`` are the blocks that stand after a run: ``key``, ``task_ids``, ``start``,
    ``end``. A block is a placement of every ledger item among its ``task_ids``: one item
    may have several blocks (a split task: ``of`` says how many), and one block several
    items (a batch: ``with`` names the others). An item's placements in ``destination`` are
    replaced by the present ones, so tracking follows when blocks are consolidated, split up
    or released. An open item with a block is ``scheduled``; one whose blocks are gone
    returns to ``backlog``. Returns the items whose state or blocks changed.
    """
    by_item: dict[str, list[dict[str, Any]]] = {}
    for b in sorted(blocks, key=lambda b: (str(b.get("start")), str(b.get("key")))):
        ids = [str(t) for t in b.get("task_ids") or []]
        for tid in ids:
            by_item.setdefault(tid, []).append({"b": b, "with": [x for x in ids if x != tid]})
    moves = []
    for row in rows(db):
        if row["state"] not in ("new", "backlog", "scheduled"):
            continue
        mine = by_item.get(row["key"], [])
        now = []
        for m in mine:
            p = {"destination": destination, "adapter": adapter, "key": m["b"]["key"], "start": m["b"].get("start"),
                 "end": m["b"].get("end"), "run": run_id}
            if len(mine) > 1:
                p["of"] = len(mine)
            if m["with"]:
                p["with"] = m["with"]
            now.append(p)
        old = [p for p in row["placements"] if p.get("destination") == destination and p.get("adapter") == adapter]
        strip = lambda ps: [{k: v for k, v in p.items() if k != "run"} for p in ps]   # noqa: E731
        state = "scheduled" if now else ("backlog" if row["state"] == "scheduled" else row["state"])
        if strip(old) == strip(now) and state == row["state"]:
            continue
        keep = [p for p in row["placements"] if p not in old]
        history = row["history"]
        if state != row["state"]:
            note = f"{len(now)} block(s)" if now else "no block"
            history = history + [{"run": run_id, "at": at, "decision": "planned", "state": state, "note": note}]
        db.execute("UPDATE item_ledger SET state=?, state_at=?, placements=?, history=? WHERE key=?",
                   (state, at if state != row["state"] else row["state_at"],
                    json.dumps(keep + now, sort_keys=True), json.dumps(history, sort_keys=True), row["key"]))
        moves.append({"key": row["key"], "title": row["title"], "was": row["state"], "state": state, "blocks": len(now)})
    return moves


def items_of_block(db: sqlite3.Connection, block_key: str) -> list[dict[str, Any]]:
    """The items a block is for: one for a whole task or a part, several for a batch."""
    return [r for r in rows(db) if any(p.get("key") == block_key for p in r["placements"])]
