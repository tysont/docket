"""Cursor advancement at close (TD §13.4, FR-APP-04).

Collection writes one proposal per source and unit with the contiguous
coverage it proved; a proposal with a gap already ends at the gap, and a unit
that proved nothing has none, so its cursor holds. ``advance`` applies a run's
proposals inside the caller's transaction: positions only move forward, and a
proposal behind the durable cursor is ignored and recorded in the
``cursor_regressions`` ledger.

A sandbox run never moves a durable cursor. An abandoned or sealed run is
never closed (the CLI refuses it first); ``advance`` refuses both again here,
so no other caller can advance cursors for a run that did not finish.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from docket.sources.cursors import apply_proposals

NOTHING: dict[str, list[Any]] = {"advanced": [], "held": [], "regressions": []}


def advance(db: sqlite3.Connection, run: Any) -> dict[str, Any]:
    """Apply the run's proposals (the caller holds the transaction). Returns what moved and why."""
    hdr = run.header
    if hdr.sandbox:
        return {**NOTHING, "skipped": "sandbox run: durable cursors are never moved"}
    if hdr.status != "open":
        return {**NOTHING, "skipped": f"{hdr.status} run: cursors advance only when an open run closes"}
    moved = apply_proposals(db, run.id)
    return {**moved, "skipped": None}
