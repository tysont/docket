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


KEPT = ("agent", "operator")  # classifications no rule overrides
_REPORT = 50                   # units named per list in a membership report; counts are always whole


def _brief(unit_id: str, name: Any, cls: Any, tier: Any) -> dict[str, Any]:
    return {"unit": unit_id, "name": name, "class": cls, "tier": None if tier is None else str(tier)}


def upsert_units(db: sqlite3.Connection, ws: Any, sid: str, units: list[Unit], seen_at: str) -> dict[str, Any]:
    """Write a complete discovery of ``sid`` into the registry and report what changed.

    Every unit listed is current. A unit of this source the discovery no longer lists is
    marked gone; one that was gone and is listed again has returned. The first discovery of
    a source is a baseline: it reports no unit as new.
    """
    rules = unit_rules(ws)
    before = {r["unit_id"]: r for r in db.execute("SELECT * FROM units WHERE source=?", (sid,)).fetchall()}
    baseline = not before
    new: list[dict[str, Any]] = []
    returned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in units:
        if u.id == ALL_UNITS:
            continue
        seen.add(u.id)
        got, used = classify(rules, sid, u, ws)
        prev = before.get(u.id)
        kind = got.get("kind", u.type)
        cls = got.get("class")
        tier = got.get("tier")
        by = "rule" if used else None
        rule = ",".join(used) or None
        if prev is not None and prev["classified_by"] in KEPT:
            kind, cls, tier, by, rule = prev["kind"], prev["class"], prev["tier"], prev["classified_by"], prev["rule"]
        first = prev["first_seen"] if prev is not None else seen_at
        db.execute("INSERT OR REPLACE INTO units (source, unit_id, name, kind, class, tier, first_seen, last_seen, "
                   "classified_by, members, rule, type, gone_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                   (sid, u.id, u.name, kind, cls, None if tier is None else str(tier), first, seen_at, by,
                    u.members, rule, u.type))
        if prev is None and not baseline:
            new.append(_brief(u.id, u.name, cls, tier))
        elif prev is not None and prev["gone_at"] is not None:
            returned.append(_brief(u.id, u.name, cls, tier))
    gone = []
    for uid, prev in sorted(before.items()):
        if uid not in seen and prev["gone_at"] is None:
            db.execute("UPDATE units SET gone_at=? WHERE source=? AND unit_id=?", (seen_at, sid, uid))
            gone.append(_brief(uid, prev["name"], prev["class"], prev["tier"]))
    return {"baseline": baseline, "current": len(seen), "new": new, "gone": gone, "returned": returned}


def membership_row(change: dict[str, Any]) -> dict[str, Any]:
    """A membership report sized for an outcome: whole counts, and the first units by name."""
    row: dict[str, Any] = {"current": change["current"], "baseline": change["baseline"]}
    for k in ("new", "gone", "returned"):
        row[k] = len(change[k])
        if change[k]:
            row[f"{k}_units"] = change[k][:_REPORT]
    return row


def set_classification(db: sqlite3.Connection, source: str, unit_id: str, values: dict[str, Any]) -> bool:
    """The operator's classification of one unit. Only the attributes given change; rules
    leave the unit alone from then on. Returns whether the unit exists."""
    row = db.execute("SELECT 1 FROM units WHERE source=? AND unit_id=?", (source, unit_id)).fetchone()
    if row is None:
        return False
    for k in ("kind", "class", "tier"):
        if k in values and values[k] is not None:
            db.execute(f"UPDATE units SET {k}=? WHERE source=? AND unit_id=?", (str(values[k]), source, unit_id))
    db.execute("UPDATE units SET classified_by='operator', rule=NULL WHERE source=? AND unit_id=?", (source, unit_id))
    return True


def unset_classification(db: sqlite3.Connection, ws: Any, source: str, unit_id: str) -> bool:
    """Give a unit back to the rules and classify it by them now. Returns whether it exists."""
    r = db.execute("SELECT * FROM units WHERE source=? AND unit_id=?", (source, unit_id)).fetchone()
    if r is None:
        return False
    utype = r["type"] or r["kind"]
    got, used = classify(unit_rules(ws), source, Unit(unit_id, r["name"], utype, r["members"]), ws)
    tier = got.get("tier")
    db.execute("UPDATE units SET kind=?, class=?, tier=?, classified_by=?, rule=? WHERE source=? AND unit_id=?",
               (got.get("kind", utype), got.get("class"), None if tier is None else str(tier),
                "rule" if used else None, ",".join(used) or None, source, unit_id))
    return True


def preview(db: sqlite3.Connection, ws: Any, source: str | None = None, gone: bool = False) -> list[dict[str, Any]]:
    """Apply the current rules to every unit in the registry and write nothing. Each row
    carries what the registry holds (``was``) and what the rules give now (``now``). A unit
    an agent classified keeps that. A row from before the registry kept types is matched
    by its kind, which was its type unless a rule had renamed it. Units that are gone are
    left out unless ``gone`` is set."""
    rules = unit_rules(ws)
    where = [w for w in ("source=?" if source else None, None if gone else "gone_at IS NULL") if w]
    sql = "SELECT * FROM units" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY source, unit_id"
    out = []
    for r in db.execute(sql, (source,) if source else ()).fetchall():
        row = dict(r)
        was = {"kind": row["kind"], "class": row["class"], "tier": row["tier"], "rule": row["rule"]}
        utype = row.get("type") or row["kind"]
        if row["classified_by"] in KEPT:
            now = dict(was)
        else:
            got, used = classify(rules, row["source"], Unit(row["unit_id"], row["name"], utype, row["members"]), ws)
            tier = got.get("tier")
            now = {"kind": got.get("kind", utype), "class": got.get("class"),
                   "tier": None if tier is None else str(tier), "rule": ",".join(used) or None}
        out.append({"source": row["source"], "unit": row["unit_id"], "name": row["name"], "type": utype,
                    "members": row["members"], "first_seen": row["first_seen"], "gone_at": row.get("gone_at"),
                    "classified_by": row["classified_by"], "was": was, "now": now})
    return out
