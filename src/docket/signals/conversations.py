"""Conversations in units without threads (TD §7.2).

Some systems give every message a thread of its own: a direct or group conversation has
no threads, yet it holds exchanges. ``signals.conversations`` names such units and the
quiet gap that ends an exchange. The items of one exchange get the same ``conversation``
key, ``<source>:<unit>:<id of its first item>``, which merge treats as their topic and a
task pack as their thread.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def unit_holds(up: Any, source: str, uid: str, units: dict[tuple[str, str], dict[str, Any]]) -> bool:
    """Whether a unit predicate holds for the unit ``uid`` of ``source``."""
    u = units.get((source, uid)) or {}
    if up.id is not None and uid not in up.id:
        return False
    if up.id_regex is not None and not re.search(up.id_regex, uid):
        return False
    if up.name_regex is not None and not re.search(up.name_regex, str(u.get("name") or "")):
        return False
    if up.kind is not None and u.get("kind") not in up.kind:
        return False
    if up.class_ is not None and u.get("class") not in up.class_:
        return False
    tier = u.get("tier")
    if up.tier is not None and str(tier) not in {str(t) for t in up.tier}:
        return False
    if up.tier_max is not None and (_num(tier) is None or _num(tier) > up.tier_max):  # type: ignore[operator]
        return False
    return True


def _instant(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def assign(items: list[dict[str, Any]], cfg: Any, units: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Set ``conversation`` on every item (``None`` where no rule names its unit). The first
    rule that names a unit decides its gap."""
    from docket.text.durations import parse_duration
    gaps: dict[tuple[str, str], timedelta | None] = {}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for it in items:
        it["conversation"] = None
        key = (str(it.get("source") or ""), str(it.get("unit") or ""))
        if key not in gaps:
            gaps[key] = next((timedelta(minutes=parse_duration(c.gap)) for c in cfg.conversations
                              if unit_holds(c.unit, key[0], key[1], units)), None)
        if gaps[key] is not None:
            groups.setdefault(key, []).append(it)
    for (source, unit), members in groups.items():
        gap = gaps[(source, unit)]
        members.sort(key=lambda it: (str(it.get("at") or ""), str(it.get("id"))))
        last: datetime | None = None
        current = ""
        for it in members:
            at = _instant(it.get("at"))
            if not current or at is None or last is None or at - last > gap:  # type: ignore[operator]
                current = f"{source}:{unit}:{it['id']}"
            it["conversation"] = current
            last = at if at is not None else last
    return items
