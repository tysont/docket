"""Scheduling windows (TD §12.4).

An answer's ``--when`` is a day, a day with a time range, ``this week``
(parsed by ``text/dates.py``), or the title of a calendar event to schedule
over. ``normalize_when`` stores the normalized text; ``resolve_window`` turns
it into a ``PlanWindow`` at plan build, when the calendar is known. Named
events are stored as ``event:<title>`` and match by case-insensitive
substring within the horizon, earliest first.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from docket.cli.errors import InputError
from docket.plan.types import DAY, Event, PlanWindow, hhmm
from docket.text.dates import _WEEKDAY_NAMES, parse_window

_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
_DATEISH = re.compile(r"^(\d{4}-\d{2}-\d{2}|today|tomorrow|next|this)$")
EVENT_PREFIX = "event:"


def normalize_when(text: str, ref: date) -> str:
    """Normalize an answer's ``--when``. A text that does not start like a date or weekday is a
    named event (``event:<title>``); one that does but does not parse is ``input.bad_window``."""
    s = " ".join(str(text).split())
    if s.lower().startswith(EVENT_PREFIX):
        name = s[len(EVENT_PREFIX):].strip()
        if not name:
            raise InputError("input.bad_window", "A named-event window needs the event's title",
                             hint="--when 'event:<title>'")
        return EVENT_PREFIX + name
    try:
        return parse_window(s, ref).normalized()
    except InputError:
        head = s.split(" ", 1)[0].lower() if s else ""
        if not s or head in _WEEKDAY_NAMES or _DATEISH.match(head):
            raise
        return EVENT_PREFIX + s


def _day_segments(d0: date, d1: date) -> tuple[tuple[date, int, int], ...]:
    out = []
    d = d0
    while d <= d1:
        out.append((d, 0, DAY))
        d += timedelta(days=1)
    return tuple(out)


def find_event(name: str, instances: list[tuple[Event, str]], lo: date, hi: date, tz: ZoneInfo) -> Event | None:
    want = name.casefold()
    hits = []
    for ev, _kind in instances:
        if want in (ev.title or "").casefold():
            hits.append(ev)

    def key(ev: Event) -> tuple[str, str]:
        s = ev.start.astimezone(tz).isoformat() if isinstance(ev.start, datetime) else ev.start.isoformat()
        return s, ev.id
    hits.sort(key=key)
    return hits[0] if hits else None


def event_window(ev: Event, label: str, tz: ZoneInfo) -> PlanWindow:
    if ev.all_day:
        d0 = ev.start if isinstance(ev.start, date) and not isinstance(ev.start, datetime) else ev.start.date()
        d1 = (ev.end if not isinstance(ev.end, datetime) else ev.end.date()) - timedelta(days=1)
        return PlanWindow(label, _day_segments(d0, max(d0, d1)), explicit_times=True, explicit_days=True,
                          event_id=ev.id)
    s = ev.start.astimezone(tz)  # type: ignore[union-attr]
    e = ev.end.astimezone(tz)  # type: ignore[union-attr]
    segs = []
    d = s.date()
    while True:
        day0 = datetime.combine(d, time(0), tzinfo=tz)
        if day0 >= e:
            break
        a = max(s, day0)
        b = min(e, datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz))
        am = a.hour * 60 + a.minute if a > day0 else 0
        bm = DAY if b.date() > d else b.hour * 60 + b.minute
        if bm > am:
            segs.append((d, am, bm))
        d += timedelta(days=1)
    return PlanWindow(label, tuple(segs), explicit_times=True, explicit_days=True, event_id=ev.id)


def resolve_window(text: str | None, ref: date, *, instances: list[tuple[Event, str]] | None = None,
                   lo: date | None = None, hi: date | None = None, tz: ZoneInfo | None = None) -> PlanWindow | None:
    """A ``PlanWindow`` for a normalized ``when``. Raises ``plan.window_unresolved`` for a named
    event the horizon does not contain, ``input.bad_window`` for text that does not parse."""
    if not text:
        return None
    s = " ".join(str(text).split())
    if s.lower().startswith(EVENT_PREFIX):
        name = s[len(EVENT_PREFIX):].strip()
        ev = find_event(name, instances or [], lo or ref, hi or ref, tz or ZoneInfo("UTC"))
        if ev is None:
            raise InputError("plan.window_unresolved", f"No calendar event in the horizon matches {name!r}.",
                             hint="Answer again with --when naming a day, a day with times, or an event title "
                                  "that is on the calendar", details={"window": s})
        return event_window(ev, s, tz or ZoneInfo("UTC"))
    m = _RANGE.match(s)
    if m:
        d0, d1 = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        return PlanWindow(s, _day_segments(d0, d1), explicit_times=False, explicit_days=False)
    if s.lower() == "this week":
        w = parse_window(s, ref)
        return PlanWindow(w.normalized(), _day_segments(w.start_date, w.end_date))
    w = parse_window(s, ref)
    if w.start is not None and w.end is not None:
        a, b = w.start.hour * 60 + w.start.minute, w.end.hour * 60 + w.end.minute
        return PlanWindow(w.normalized(), ((w.start_date, a, b),), explicit_times=True, explicit_days=True)
    if w.start_date != w.end_date:
        return PlanWindow(w.normalized(), _day_segments(w.start_date, w.end_date))
    return PlanWindow(w.normalized(), _day_segments(w.start_date, w.start_date), explicit_days=True)


def describe(w: PlanWindow) -> str:
    parts = [f"{d:%a %Y-%m-%d} {hhmm(s)}-{hhmm(e)}" for d, s, e in w.segments[:3]]
    return ", ".join(parts) + (" ..." if len(w.segments) > 3 else "")


def window_free_minutes(free: list[tuple[int, int]]) -> tuple[int, int]:
    """(total, longest) free minutes of a list of intervals."""
    total = sum(e - s for s, e in free)
    longest = max((e - s for s, e in free), default=0)
    return total, longest


__all__: list[Any] = ["normalize_when", "resolve_window", "event_window", "describe", "EVENT_PREFIX"]
