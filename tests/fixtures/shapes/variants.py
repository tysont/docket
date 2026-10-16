"""Shape variation generators (Test Plan §5.6).

Variants are generated from a source's own response contract and applied to one entry of
one recorded response, so every other unit's recording is untouched:

- ``drop_optional``   remove each optional field
- ``drop_required``   remove each required field
- ``unknown_enum``    replace each enumerated field with a value outside its set
- ``type_change``     replace each field with a value of another type
- ``empty``           empty the entries list of the page
- ``nameless``        remove a unit's display name
- ``unknown_field``   add a field the contract does not name

Calendar variants (``calendar_variants``) work on a JSON calendar fixture: a dropped title,
a dropped start, an unknown status, a type change, an empty calendar, an injected tombstone,
a long recurring multi-day span and an unknown property.

Each variant states the shapes it may record (``allowed``), the shape it must record
(``required``, or None) and how the affected unit must end (``unit``).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable

UNKNOWN_ENUM = "ZZ_UNKNOWN_VALUE"
EXTRA_FIELD = "zz_extra_field"


@dataclass
class Variant:
    name: str
    kind: str
    recording: str                       # recordings file (relative to the workspace)
    match: Callable[[dict[str, Any]], bool]   # selects the recording line by its request
    entries: str                         # dotted path of the entries list in body_json
    index: int                           # which entry
    mutate: Callable[[Any], Any]         # entry -> entry (or entries list -> list for ``empty``)
    unit: str | None                     # the affected unit id (None: the source has one unit)
    required: set[tuple[str, str]] = field(default_factory=set)
    allowed: set[tuple[str, str]] = field(default_factory=set)
    unit_outcome: str = "read"           # read | failed | empty | skipped
    whole_list: bool = False

    def apply(self, lines: list[str]) -> list[str]:
        out = []
        hits = 0
        for ln in lines:
            row = json.loads(ln)
            if self.match(row["request"]):
                hits += 1
                body = row["response"]["body_json"]
                parts = self.entries.split(".")
                holder = body
                for p in parts[:-1]:
                    holder = holder[p]
                if self.whole_list:
                    holder[parts[-1]] = self.mutate(holder[parts[-1]])
                else:
                    holder[parts[-1]][self.index] = self.mutate(copy.deepcopy(holder[parts[-1]][self.index]))
                ln = json.dumps(row, sort_keys=True)
            out.append(ln)
        assert hits == 1, f"{self.name}: the selector matched {hits} recording lines"
        return out


def _drop(path: str) -> Callable[[Any], Any]:
    def f(e: Any) -> Any:
        parts = path.split(".")
        cur = e
        for p in parts[:-1]:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
        return e
    return f


def _set(path: str, value: Any) -> Callable[[Any], Any]:
    def f(e: Any) -> Any:
        parts = path.split(".")
        cur = e
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        return e
    return f


OTHER_TYPE = {"str": 12345, "timestamp": {"zz": 1},  # an epoch number is a valid timestamp; an object is not
               "int": "many", "float": "many", "number": "many", "bool": "yes",
              "list": "x", "dict": "x", "any": None}
TYPE_NAME = {int: "int", str: "str", dict: "dict", list: "list", bool: "bool"}


def contract_variants(*, sid: str, contract: Any, kind: str, recording: str, match: Callable, entries: str,
                      index: int, unit: str | None, mapping: dict[str, str], unit_kind: bool = False,
                      unit_id_field: str | None = None, unit_name_field: str | None = None) -> list[Variant]:
    """Variants of one entry, from the contract of ``kind`` (a ``ContractKind``)."""
    out: list[Variant] = []
    c = contract

    def mapped_missing(f: str) -> set[tuple[str, str]]:
        return {("item.mapping", f"{sid}.{k} missing") for k, p in mapping.items() if p == "$." + f}

    def base(name: str, vkind: str, mutate: Callable, **kw: Any) -> Variant:
        return Variant(f"{sid}.{kind}:{name}", vkind, recording, match, entries, index, mutate, unit, **kw)

    for f, typ in sorted(c.optional.items()):
        var = c.variations.get(f)
        rec = {("source.contract", f"{sid}.{kind}.{f} missing")} if var and var.missing and var.missing.record else set()
        out.append(base(f"drop {f}", "drop_optional", _drop(f), required=rec, allowed=rec | mapped_missing(f)))
        other = OTHER_TYPE.get(typ)
        if other is not None:
            detail = f"{sid}.{kind}.{f} type {typ}->{TYPE_NAME[type(other)]}"
            out.append(base(f"retype {f}", "type_change", _set(f, other), required={("source.contract", detail)},
                            allowed={("source.contract", detail)} | mapped_missing(f)))
    for f, typ in sorted(c.required.items()):
        if unit_kind and f == unit_id_field:  # the unit's id: the contract skips the unit record
            sh = ("source.contract", f"{sid}.{kind}.{f} missing")
            out.append(base(f"drop {f}", "drop_required", _drop(f), required={sh},
                            allowed={sh, ("source.unit", f"{sid} unit without id")}, unit_outcome="skipped"))
            continue
        sh = ("source.contract", f"{sid}.{kind}.{f} missing")
        out.append(base(f"drop {f}", "drop_required", _drop(f), required={sh}, allowed={sh}))
        other = OTHER_TYPE.get(typ)
        if other is not None and not unit_kind:
            detail = f"{sid}.{kind}.{f} type {typ}->{TYPE_NAME[type(other)]}"
            out.append(base(f"retype {f}", "type_change", _set(f, other), required={("source.contract", detail)},
                            allowed={("source.contract", detail)}, unit_outcome="failed"))
    for f in sorted(c.enums):
        sh = ("source.contract", f"{sid}.{kind}.{f}={UNKNOWN_ENUM}")
        out.append(base(f"unknown {f}", "unknown_enum", _set(f, UNKNOWN_ENUM), required={sh}, allowed={sh}))
    extra = ("source.contract", f"{sid}.{kind} field {EXTRA_FIELD} not in contract")
    rec = {extra} if c.unknown_fields == "record" else set()
    out.append(base("unknown field", "unknown_field", _set(EXTRA_FIELD, 1), required=rec, allowed=rec))
    if unit_kind and unit_name_field:
        sh = ("source.unit", f"{sid} unit without name")
        out.append(base(f"nameless ({unit_name_field})", "nameless", _drop(unit_name_field), allowed={sh}))
    if not unit_kind:
        v = base("empty page", "empty", lambda rows: [], unit_outcome="empty")
        v.whole_list = True
        out.append(v)
    return out


# --- calendar ---------------------------------------------------------------------------------


@dataclass
class CalendarVariant:
    name: str
    kind: str
    mutate: Callable[[dict[str, Any]], dict[str, Any]]
    allowed: set[tuple[str, str]] = field(default_factory=set)    # (area, detail prefix)
    required: set[tuple[str, str]] = field(default_factory=set)


def _event(cal: dict[str, Any], eid: str) -> dict[str, Any]:
    return next(e for e in cal["events"] if e.get("id") == eid)


def calendar_variants(target: str, series: str) -> list[CalendarVariant]:
    """Variants of one event (``target``) and of one recurring series (``series``)."""
    malformed = ("plan.calendar", "malformed event: KeyError")

    def drop(f: str) -> Callable:
        def m(cal: dict[str, Any]) -> dict[str, Any]:
            _event(cal, target).pop(f, None)
            return cal
        return m

    def setf(f: str, v: Any) -> Callable:
        def m(cal: dict[str, Any]) -> dict[str, Any]:
            _event(cal, target)[f] = v
            return cal
        return m

    def tombstone(cal: dict[str, Any]) -> dict[str, Any]:
        s = _event(cal, series)
        cal["events"].append({"id": f"{series}-zz-x", "series_id": series, "status": "cancelled",
                              "original_start": s["start"].replace(s["start"][:10], "2026-10-22")})
        return cal

    def long_span(cal: dict[str, Any]) -> dict[str, Any]:
        cal["events"].append({"id": "zz-long-span", "calendar": "work", "title": "Offsite block",
                              "start": "2026-08-17T09:00:00", "end": "2026-08-19T17:00:00",
                              "recurrence": {"freq": "weekly", "byday": ["mon"]}})
        return cal

    def empty(cal: dict[str, Any]) -> dict[str, Any]:
        cal["events"] = []
        return cal

    return [
        CalendarVariant("drop title", "drop_optional", drop("title"),
                        allowed={("plan.calendar", "unclassified event: ")}),
        CalendarVariant("drop start", "drop_required", drop("start"), required={malformed}, allowed={malformed}),
        CalendarVariant("unknown status", "unknown_enum", setf("status", UNKNOWN_ENUM)),
        CalendarVariant("retype start", "type_change", setf("start", 12345),
                        allowed={("plan.calendar", "malformed event: TypeError"),
                                 ("plan.calendar", "malformed event: ValueError")}),
        CalendarVariant("empty calendar", "empty", empty),
        CalendarVariant("tombstone", "tombstone", tombstone),
        CalendarVariant("long recurring span", "long_span", long_span,
                        allowed={("plan.calendar", "unclassified event: ")}),
        CalendarVariant("unknown property", "unknown_field", setf(EXTRA_FIELD, 1)),
    ]
