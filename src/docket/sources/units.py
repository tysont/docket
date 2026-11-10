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


def _mentions(want: Any, unit: Unit, ws: Any) -> bool:
    """Whether the unit's name mentions the profile vocabulary a rule asks for."""
    if ws is None:
        return False
    from docket.items.identity import ProfileIndex
    idx = getattr(ws, "_unit_profile_index", None)
    if idx is None:
        idx = ProfileIndex(ws.config.profile)
        try:
            ws._unit_profile_index = idx  # one index per loaded workspace, not per unit
        except AttributeError:
            pass
    found = idx.mentions(unit.name or "")
    if want.topics is not None:
        hit = found["topics"]
        if want.topics == "owned":
            hit = [t for t in hit if idx.topics[t].owned]
        elif want.topics != "any":
            hit = [t for t in hit if t in want.topics]
        if not hit:
            return False
    if want.entities is not None:
        hit = found["entities"]
        if want.entities in ("strategic", "protected"):
            hit = [e for e in hit if getattr(idx.entities[e], want.entities)]
        elif want.entities != "any":
            hit = [e for e in hit if e in want.entities]
        if not hit:
            return False
    return True


def matches(rule: UnitRule, sid: str, unit: Unit, ws: Any = None) -> bool:
    m = rule.match
    if m.mentions is not None and not _mentions(m.mentions, unit, ws):
        return False
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


def classify(rules: list[UnitRule], sid: str, unit: Unit, ws: Any = None) -> tuple[dict[str, Any], list[str]]:
    """Return ({kind, class, tier}, ids of the rules that set them). ``ws`` gives rules that
    use the profile's vocabulary their profile; without it such a rule does not match."""
    got: dict[str, Any] = {}
    used: list[str] = []
    for i, rule in enumerate(rules):
        if not matches(rule, sid, unit, ws):
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
        got, used = classify(rules, sid, u, ws)
        prev = db.execute("SELECT * FROM units WHERE source=? AND unit_id=?", (sid, u.id)).fetchone()
        kind = got.get("kind", u.type)
        cls = got.get("class")
        tier = got.get("tier")
        by = "rule" if used else None
        if prev is not None and prev["classified_by"] == "agent":
            kind, cls, tier, by = prev["kind"], prev["class"], prev["tier"], "agent"
        first = prev["first_seen"] if prev is not None else seen_at
        db.execute("INSERT OR REPLACE INTO units (source, unit_id, name, kind, class, tier, first_seen, last_seen, "
                   "classified_by, members, rule, type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, u.id, u.name, kind, cls, None if tier is None else str(tier), first, seen_at, by,
                    u.members, ",".join(used) or None, u.type))
        out.append({"unit": u.id, "kind": kind, "class": cls, "tier": tier, "classified_by": by})
    return out


def preview(db: sqlite3.Connection, ws: Any, source: str | None = None) -> list[dict[str, Any]]:
    """Apply the current rules to every unit in the registry and write nothing. Each row
    carries what the registry holds (``was``) and what the rules give now (``now``). A unit
    an agent classified keeps that. A row from before the registry kept types is matched
    by its kind, which was its type unless a rule had renamed it."""
    rules = unit_rules(ws)
    sql = "SELECT * FROM units" + (" WHERE source=?" if source else "") + " ORDER BY source, unit_id"
    out = []
    for r in db.execute(sql, (source,) if source else ()).fetchall():
        row = dict(r)
        was = {"kind": row["kind"], "class": row["class"], "tier": row["tier"], "rule": row["rule"]}
        utype = row.get("type") or row["kind"]
        if row["classified_by"] == "agent":
            now = dict(was)
        else:
            got, used = classify(rules, row["source"], Unit(row["unit_id"], row["name"], utype, row["members"]), ws)
            tier = got.get("tier")
            now = {"kind": got.get("kind", utype), "class": got.get("class"),
                   "tier": None if tier is None else str(tier), "rule": ",".join(used) or None}
        out.append({"source": row["source"], "unit": row["unit_id"], "name": row["name"], "type": utype,
                    "members": row["members"], "first_seen": row["first_seen"], "was": was, "now": now})
    return out
