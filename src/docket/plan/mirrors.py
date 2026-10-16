"""Mirrors (TD §12.5): opaque holds copied from one calendar to another.

Each run lists the source calendar's events in the horizon and compares them
with the owned mirror holds on the target calendar, matched by a key derived
from the source event's key. Per source event: an equal hold is ``keep``, a
hold at another time is ``move``, no hold is ``create`` unless the operator
deleted it. A hold whose source event is gone is ``release``.

Deletions are remembered in a ledger: a hold Docket has seen on the calendar
(state ``present``) that is now missing while its source still exists was
deleted by the operator, and is not recreated when ``respect_deletions`` is
set. Counts always reconcile: ``qualifying == created + moved + kept + suppressed``.

``plan_mirrors`` is pure. The plan stage reads the ledger before and writes
``ledger_updates`` after a build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from docket.plan.types import Event, mirror_key

LEDGER = "mirror_holds"


@dataclass
class MirrorPlan:
    ops: list[dict[str, Any]] = field(default_factory=list)
    holds: list[Event] = field(default_factory=list)       # holds that stand after the plan
    ledger_updates: list[tuple[str, str]] = field(default_factory=list)   # (key, state)
    counts: dict[str, int] = field(default_factory=dict)
    owned_keys: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)


def _iso(v: datetime | date, tz: ZoneInfo) -> str:
    return v.astimezone(tz).isoformat() if isinstance(v, datetime) else v.isoformat()


def plan_mirrors(rules: list[Any], instances: list[tuple[Event, str]], *, prefix: str, tz: ZoneInfo,
                 ledger: dict[str, str] | None = None) -> MirrorPlan:
    """``rules``: ``planning.mirrors`` entries. ``instances``: classified concrete events of the
    horizon. ``ledger``: latest state per hold key (``present`` or ``released``)."""
    ledger = ledger or {}
    out = MirrorPlan()
    c = {"qualifying": 0, "created": 0, "moved": 0, "kept": 0, "suppressed": 0, "released": 0}
    holds_all: dict[str, Event] = {}
    duplicates: list[Event] = []
    for ev, kind in instances:
        if kind == "owned" and ev.owned_key and ev.owned_key.startswith(prefix + "m_"):
            out.owned_keys.append(ev.owned_key)
            if ev.owned_key in holds_all:
                duplicates.append(ev)
            else:
                holds_all[ev.owned_key] = ev
    out.owned_keys.sort()
    claimed: set[str] = set()
    for rule in rules:
        src_cal, dst_cal = rule.from_, rule.to
        title, transp = rule.as_.title, rule.as_.transparency
        sources = sorted((ev for ev, kind in instances if ev.calendar == src_cal and kind != "owned"
                          and ev.transparency != "transparent" and (rule.include_all_day or not ev.all_day)),
                         key=lambda e: (_iso(e.start, tz), e.id))
        for ev in sources:
            key = mirror_key(prefix, dst_cal, ev.id)
            if key in claimed:
                continue
            claimed.add(key)
            c["qualifying"] += 1
            start, end = _iso(ev.start, tz), _iso(ev.end, tz)
            op = {"calendar": dst_cal, "key": key, "start": start, "end": end, "title": title, "notes": "",
                  "transparency": transp, "kind": "mirror", "source": ev.id, "task_ids": []}
            hold = holds_all.get(key)
            if hold is not None:
                same = _iso(hold.start, tz) == start and _iso(hold.end, tz) == end
                if same:
                    c["kept"] += 1
                    out.ops.append({"verb": "keep", **op})
                else:
                    c["moved"] += 1
                    out.ops.append({"verb": "move", **op, "from": {"start": _iso(hold.start, tz),
                                                                   "end": _iso(hold.end, tz)}})
                out.holds.append(ev)
                out.ledger_updates.append((key, "present"))
            elif rule.respect_deletions and ledger.get(key) == "present":
                c["suppressed"] += 1
                out.suppressed.append(key)
            else:
                c["created"] += 1
                out.ops.append({"verb": "create", **op})
                out.holds.append(ev)
    gone = [(holds_all[k], "source event is gone") for k in sorted(holds_all) if k not in claimed]
    for hold, reason in gone + [(d, "duplicate hold") for d in duplicates]:
        key = hold.owned_key or ""
        c["released"] += 1
        out.ops.append({"verb": "release", "calendar": hold.calendar, "key": key, "start": _iso(hold.start, tz),
                        "end": _iso(hold.end, tz), "title": hold.title, "notes": hold.notes or "", "kind": "mirror",
                        "reason": reason, "event_id": hold.id, "task_ids": []})
        if reason != "duplicate hold":
            out.ledger_updates.append((key, "released"))
    out.ops.sort(key=lambda o: (o["start"], o["key"], o["verb"]))
    out.counts = c
    return out
