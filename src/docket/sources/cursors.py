"""Cursor models, positions and proposals (TD §6.4, §13.4).

Positions are strings. Two positions compare numerically when both are
integers, by instant when both are ISO-8601 timestamps, and are otherwise
treated as incomparable (an opaque page token): an incomparable proposal is
taken as forward, because only an order Docket knows can prove a regression.

During collection each source and unit writes a ``cursor_proposal`` with the
contiguous coverage it proved, from the durable cursor forward; a proposal
with a gap ends at the gap, and a unit that proved nothing writes none (its
cursor holds). Within a run a proposal only moves forward, so a refresh
generation extends the coverage of the generation before it. ``docket close``
applies proposals in one transaction; a proposal behind the durable cursor is
ignored and recorded in the ``cursor_regressions`` ledger.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from docket.config.source_models import CursorConfig

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _as_int(v: str) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_instant(v: str) -> datetime | None:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def compare(a: Any, b: Any) -> int | None:
    """-1, 0, 1, or None when the positions are incomparable."""
    if a is None or b is None:
        return None
    a, b = str(a), str(b)
    if a == b:
        return 0
    ia, ib = _as_int(a), _as_int(b)
    if ia is not None and ib is not None:
        return (ia > ib) - (ia < ib)
    ta, tb = _as_instant(a), _as_instant(b)
    if ta is not None and tb is not None:
        return (ta > tb) - (ta < tb)
    return None


def is_behind(proposed: Any, durable: Any) -> bool:
    c = compare(proposed, durable)
    return c is not None and c < 0


def is_ahead(proposed: Any, current: Any) -> bool:
    if current is None:
        return proposed is not None
    c = compare(proposed, current)
    return c is None or c > 0


def max_key(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    c = compare(a, b)
    return b if c is not None and c < 0 else a


def interval_from(cfg: CursorConfig, instant: str, coverage_end: str | None) -> str | None:
    if coverage_end:
        return coverage_end
    if cfg.lookback:
        from docket.text.durations import parse_duration
        start = datetime.fromisoformat(instant) - timedelta(minutes=parse_duration(cfg.lookback))
        return start.isoformat()
    return None


def cadence_skip(cfg: CursorConfig, run_date: date, last_read: str | None) -> str | None:
    """The policy that skips this run, or None to read. A skipped day is never a gap."""
    if cfg.model != "cadence":
        return None
    if cfg.weekdays and WEEKDAYS[run_date.weekday()] not in cfg.weekdays:
        return f"cadence: reads on {', '.join(cfg.weekdays)}"
    if cfg.every_days and last_read:
        last = _as_instant(last_read)
        if last is not None and (run_date - last.date()).days < cfg.every_days:
            return f"cadence: every {cfg.every_days} day(s); last read {last.date().isoformat()}"
    return None


# --- proposals --------------------------------------------------------------------


def durable(db: sqlite3.Connection, sources: list[str]) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    out: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for s in sources:
        for row in db.execute("SELECT unit, position, coverage_end FROM cursors WHERE source=?", (s,)):
            out[(s, row["unit"])] = (row["position"], row["coverage_end"])
    return out


def proposals(db: sqlite3.Connection, run_id: str, sources: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for s in sources:
        for row in db.execute("SELECT * FROM cursor_proposals WHERE run_id=? AND source=?", (run_id, s)):
            out[(s, row["unit"])] = dict(row)
    return out


def processed_ids(db: sqlite3.Connection, source: str) -> set[str]:
    return {r[0] for r in db.execute("SELECT record_id FROM processed_ids WHERE source=?", (source,))}


def write_proposal(db: sqlite3.Connection, run_id: str, source: str, unit: str, position: str | None,
                   coverage_end: str | None, *, generation: int, model: str, complete: bool) -> bool:
    """Record proven coverage; within a run a proposal only moves forward. Returns whether it changed."""
    if position is None:
        return False
    row = db.execute("SELECT position, complete FROM cursor_proposals WHERE run_id=? AND source=? AND unit=?",
                     (run_id, source, unit)).fetchone()
    if row is not None:
        same = compare(position, row["position"]) == 0
        if not (is_ahead(position, row["position"]) or (same and complete and not row["complete"])):
            return False
    db.execute("INSERT OR REPLACE INTO cursor_proposals (run_id, source, unit, position, coverage_end, generation, "
               "model, complete) VALUES (?,?,?,?,?,?,?,?)",
               (run_id, source, unit, str(position), coverage_end, generation, model, int(complete)))
    return True


def apply_proposals(db: sqlite3.Connection, run_id: str) -> dict[str, list[Any]]:
    """Apply a run's proposals (the caller holds the transaction). Forward only."""
    advanced: set[str] = set()
    held: set[str] = set()
    regressions: list[dict[str, Any]] = []
    for p in db.execute("SELECT * FROM cursor_proposals WHERE run_id=? ORDER BY source, unit", (run_id,)).fetchall():
        src, unit = p["source"], p["unit"]
        if p["model"] == "id-scan":
            db.execute("INSERT OR IGNORE INTO processed_ids (source, record_id, run_id) VALUES (?,?,?)",
                       (src, unit, run_id))
            advanced.add(src)
            continue
        cur = db.execute("SELECT position FROM cursors WHERE source=? AND unit=?", (src, unit)).fetchone()
        cur_pos = cur["position"] if cur else None
        if cur_pos is not None and is_behind(p["position"], cur_pos):
            row = {"source": src, "unit": unit, "proposed": p["position"], "durable": cur_pos}
            regressions.append(row)
            held.add(src)
            db.execute("INSERT INTO ledgers (ledger, run_id, key, json) VALUES (?,?,?,?)",
                       ("cursor_regressions", run_id, f"{src}/{unit}", json.dumps(row, sort_keys=True)))
            continue
        db.execute("INSERT OR REPLACE INTO cursors (source, unit, position, coverage_end, updated_run) "
                   "VALUES (?,?,?,?,?)", (src, unit, p["position"], p["coverage_end"], run_id))
        advanced.add(src)
    return {"advanced": sorted(advanced), "held": sorted(held), "regressions": regressions}
