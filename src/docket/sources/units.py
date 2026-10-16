"""Unit registry and rule classification (TD §6.9).

Discovered units are upserted into the ``units`` table. ``signals.units.rules``
assign ``kind``, ``class`` and ``tier`` from name patterns, id patterns,
membership counts and unit type; rules apply in order and each attribute takes
the first rule that sets it. A unit no rule classifies keeps its unit type as
its kind and has no class; classification by an agent task arrives with the
``classify`` task kind (M4), and a unit already classified by an agent keeps
that classification.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from docket.config.source_models import UnitRule, UnitSignals
from docket.sources.base import ALL_UNITS, Unit


def unit_rules(ws: Any) -> list[UnitRule]:
    raw = (ws.config.signals or {}).get("units")
    return UnitSignals.model_validate(raw).rules if raw else []


def matches(rule: UnitRule, sid: str, unit: Unit) -> bool:
    m = rule.match
    if m.source and m.source != sid:
        return False
    if m.name_regex and not re.search(m.name_regex, unit.name or ""):
        return False
    if m.id_regex and not re.search(m.id_regex, unit.id):
        return False
    if m.type and (unit.type or "") not in m.type:
        return False
    if m.members_min is not None and (unit.members is None or unit.members < m.members_min):
        return False
    if m.members_max is not None and (unit.members is None or unit.members > m.members_max):
        return False
    return True


def classify(rules: list[UnitRule], sid: str, unit: Unit) -> tuple[dict[str, Any], list[str]]:
    """Return ({kind, class, tier}, ids of the rules that set them)."""
    got: dict[str, Any] = {}
    used: list[str] = []
    for i, rule in enumerate(rules):
        if not matches(rule, sid, unit):
            continue
        took = False
        for k, v in rule.set.items():
            if k not in got:
                got[k] = v
                took = True
        if took:
            used.append(rule.id or f"rule[{i}]")
    return got, used


def upsert_units(db: sqlite3.Connection, ws: Any, sid: str, units: list[Unit], seen_at: str) -> list[dict[str, Any]]:
    rules = unit_rules(ws)
    out = []
    for u in units:
        if u.id == ALL_UNITS:
            continue
        got, used = classify(rules, sid, u)
        prev = db.execute("SELECT * FROM units WHERE source=? AND unit_id=?", (sid, u.id)).fetchone()
        kind = got.get("kind", u.type)
        cls = got.get("class")
        tier = got.get("tier")
        by = "rule" if used else None
        if prev is not None and prev["classified_by"] == "agent":
            kind, cls, tier, by = prev["kind"], prev["class"], prev["tier"], "agent"
        first = prev["first_seen"] if prev is not None else seen_at
        db.execute("INSERT OR REPLACE INTO units (source, unit_id, name, kind, class, tier, first_seen, last_seen, "
                   "classified_by, members, rule) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, u.id, u.name, kind, cls, None if tier is None else str(tier), first, seen_at, by,
                    u.members, ",".join(used) or None))
        out.append({"unit": u.id, "kind": kind, "class": cls, "tier": tier, "classified_by": by})
    return out
