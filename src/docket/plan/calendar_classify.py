"""Calendar classification (TD §12.1).

``classify_calendar`` turns provider events into per-day ``Segment`` lists:

1. Recurring masters expand within the horizon only; an exception instance
   replaces its generated occurrence, and a cancelled one (a tombstone)
   removes it. Other cancelled events are ignored. Counts stay consistent:
   ``considered == concrete + ignored``.
2. Each concrete event gets one kind. Owned comes first, from the ownership
   key; then ``profile.calendar.classify`` rules in order; an event no rule
   matches is a ``meeting`` (busy) and records an unexpected shape with its
   title reduced to its word shape.
3. Each event is clipped per local day; a span longer than a day becomes one
   segment per day inside the horizon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from docket.plan.types import DAY, WEEKDAYS, Event, Segment
from docket.shapes.unexpected import unexpected

KINDS = ("meeting", "focus", "soft_hold", "out_of_office", "boundary", "mirrored", "owned", "informational")


# --- word shape ----------------------------------------------------------------


def _word_shape(w: str) -> str:
    if w.isdigit():
        return "9"
    if w.isalpha():
        if w.islower():
            return "aa"
        if w.isupper():
            return "AA" if len(w) > 1 else "A"
        if w[0].isupper() and w[1:].islower():
            return "Aa"
    out: list[str] = []
    for ch in w:
        c = "9" if ch.isdigit() else ("A" if ch.isupper() else ("a" if ch.isalpha() else ch))
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


def word_shape(title: str) -> str:
    """``"Lunch with 9"`` -> ``"Aa aa 9"``: the title's shape without its words."""
    return " ".join(_word_shape(w) for w in str(title).split()) or "(empty)"


# --- recurrence ------------------------------------------------------------------


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date() if "T" in s else date.fromisoformat(s[:10])
    except ValueError:
        return None


def _occurrence_days(rule: dict[str, Any], first: date, lo: date, hi: date) -> Iterable[date]:
    """Days of a series (starting ``first``) inside ``[lo, hi)``. ``count`` counts from the series start."""
    freq = str(rule.get("freq", "weekly")).lower()
    interval = max(1, int(rule.get("interval") or 1))
    count = rule.get("count")
    count = int(count) if count is not None else None
    until = _as_date(rule.get("until"))
    last = hi if until is None else min(hi, until + timedelta(days=1))
    if freq == "daily":
        k = 0 if count is not None else max(0, (lo - first).days // interval)
        n = k
        while True:
            d = first + timedelta(days=k * interval)
            if d >= last or (count is not None and n >= count):
                return
            if d >= lo:
                yield d
            k += 1
            n += 1
    if freq != "weekly":
        unexpected("plan.calendar", f"recurrence freq {freq}", reading="treat the master as a single event",
                   extend="docket/plan/calendar_classify.py: _occurrence_days")
        if lo <= first < last:
            yield first
        return
    by = [WEEKDAYS.index(str(d).lower()[:3]) for d in (rule.get("byday") or []) if str(d).lower()[:3] in WEEKDAYS]
    by = sorted(set(by)) or [first.weekday()]
    week0 = first - timedelta(days=first.weekday())
    w = 0 if count is not None else max(0, (lo - week0).days // (7 * interval) - 1)
    n = 0
    while True:
        base = week0 + timedelta(days=7 * w * interval)
        if base >= last:
            return
        for wd in by:
            d = base + timedelta(days=wd)
            if d < first:
                continue
            if count is not None and n >= count:
                return
            n += 1
            if d >= last:
                return
            if d >= lo:
                yield d
        w += 1


def _shift(ev: Event, day: date, tz: ZoneInfo) -> tuple[datetime | date, datetime | date]:
    if ev.all_day:
        span = _as_date(ev.end) - _as_date(ev.start)  # type: ignore[operator]
        return day, day + span
    s = ev.start.astimezone(tz)  # type: ignore[union-attr]
    dur = ev.end - ev.start  # type: ignore[operator]
    start = datetime.combine(day, s.time().replace(tzinfo=None), tzinfo=tz)
    return start, start + dur


@dataclass
class Expansion:
    """Concrete events plus an account of every input: ``considered == masters + exceptions +
    singles + ignored``. ``tombstones`` counts cancelled inputs (a subset of the accounting)."""
    events: list[Event]
    considered: int = 0
    masters: int = 0
    exceptions: int = 0
    singles: int = 0
    ignored: int = 0
    generated: int = 0
    tombstones: int = 0


def _overlaps(ev: Event, lo: date, hi: date, tz: ZoneInfo) -> bool:
    if ev.all_day:
        return _as_date(ev.start) < hi and _as_date(ev.end) > lo  # type: ignore[operator]
    s = ev.start.astimezone(tz)  # type: ignore[union-attr]
    e = ev.end.astimezone(tz)  # type: ignore[union-attr]
    return s < datetime.combine(hi, time(0), tzinfo=tz) and e > datetime.combine(lo, time(0), tzinfo=tz)


def _okey(v: Any, tz: ZoneInfo) -> str:
    if isinstance(v, datetime):
        return v.astimezone(tz).isoformat()
    return v.isoformat() if v is not None else ""


def expand(events: list[Event], lo: date, hi: date, tz: ZoneInfo) -> Expansion:
    """Concrete, non-cancelled events touching ``[lo, hi)``. Masters expand within the range only."""
    out = Expansion(events=[], considered=len(events))
    exceptions: dict[tuple[str, str], Event] = {}
    singles: list[Event] = []
    masters: list[Event] = []
    for ev in events:
        if ev.series_id and ev.original_start is not None:
            k = (ev.series_id, _okey(ev.original_start, tz))
            if k in exceptions:  # a duplicate exception: the later one wins, the earlier is ignored
                out.ignored += 1
            exceptions[k] = ev
        elif ev.recurrence:
            masters.append(ev)
        else:
            singles.append(ev)
    used: set[tuple[str, str]] = set()
    for m in masters:
        if m.cancelled:
            out.ignored += 1
            out.tombstones += 1
            continue
        out.masters += 1
        first = _as_date(m.start if m.all_day else m.start.astimezone(tz))  # type: ignore[union-attr]
        span_days = max(1, ((_as_date(m.end) or first) - first).days + 1)  # type: ignore[operator]
        for d in _occurrence_days(m.recurrence or {}, first, lo - timedelta(days=span_days), hi):
            s, e = _shift(m, d, tz)
            key = (m.id, _okey(s, tz))
            exc = exceptions.get(key)
            if exc is not None:
                used.add(key)
                if exc.cancelled:
                    continue
                inst = replace(exc, recurring=True)
            else:
                inst = replace(m, id=f"{m.id}@{_okey(s, tz)}", start=s, end=e, recurrence=None, series_id=m.id,
                               original_start=s, recurring=True)
            if _overlaps(inst, lo, hi, tz):
                out.generated += 1
                out.events.append(inst)
    for key, exc in exceptions.items():
        if key in used:
            out.exceptions += 1
            if exc.cancelled:
                out.tombstones += 1
            continue
        singles.append(exc)  # an exception whose occurrence is not generated stands alone
    for ev in singles:
        if ev.cancelled:
            out.tombstones += 1
            out.ignored += 1
            continue
        if not _overlaps(ev, lo, hi, tz):
            out.ignored += 1
            continue
        out.singles += 1
        out.events.append(ev)
    out.events.sort(key=lambda e: (_okey(e.start, tz), e.id))
    return out


# --- classification ------------------------------------------------------------


def _prop(ev: Event, name: str) -> Any:
    if name in ev.properties:
        return ev.properties[name]
    if name in ("transparency", "calendar", "status", "all_day", "recurring", "title"):
        return getattr(ev, name)
    return None


def _prop_matches(actual: Any, want: Any) -> bool:
    if want is True:
        return bool(actual)
    if want is False:
        return not actual
    if isinstance(want, str) and isinstance(actual, str):
        return actual.casefold() == want.casefold()
    return actual == want


class Classifier:
    def __init__(self, rules: list[Any], key_prefix: str) -> None:
        self.prefix = key_prefix
        self.rules = []
        for r in rules:
            m = r.match
            rx = re.compile(m.title_regex) if m.title_regex else None
            self.rules.append((rx, dict(m.property), m.calendar, m.transparency, m.all_day, r.kind))

    def kind(self, ev: Event, *, record: bool = True) -> str:
        if ev.owned_key and ev.owned_key.startswith(self.prefix):
            return "owned"
        for rx, props, cal, transp, all_day, kind in self.rules:
            if rx is not None and not rx.search(ev.title or ""):
                continue
            if cal is not None and ev.calendar != cal:
                continue
            if transp is not None and ev.transparency != transp:
                continue
            if all_day is not None and ev.all_day != all_day:
                continue
            if any(not _prop_matches(_prop(ev, k), v) for k, v in props.items()):
                continue
            return kind
        if record:
            shape = word_shape(ev.title)
            unexpected("plan.calendar", f"unclassified event: {shape}", sample={"title_shape": shape,
                                                                               "all_day": ev.all_day},
                       reading="treat as a meeting (busy)", extend="profile.calendar.classify")
        return "meeting"


def clip(ev: Event, kind: str, lo: date, hi: date, tz: ZoneInfo) -> list[Segment]:
    """Per-day segments of ``ev`` inside ``[lo, hi)``."""
    out: list[Segment] = []
    if ev.all_day:
        d, end = max(_as_date(ev.start), lo), min(_as_date(ev.end), hi)  # type: ignore[type-var]
        while d < end:
            out.append(Segment(d, 0, DAY, kind, ev.id, ev.title, ev.calendar, ev.owned_key))
            d += timedelta(days=1)
        return out
    s = ev.start.astimezone(tz)  # type: ignore[union-attr]
    e = ev.end.astimezone(tz)  # type: ignore[union-attr]
    if e <= s:
        return out
    d = max(s.date(), lo)
    while d < hi:
        day_start = datetime.combine(d, time(0), tzinfo=tz)
        if day_start >= e:
            break
        a = max(s, day_start)
        b = min(e, datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz))
        am = int((a - day_start).total_seconds() // 60) if a > day_start else 0
        bm = DAY if b.date() > d else b.hour * 60 + b.minute
        if bm > am:
            out.append(Segment(d, am, bm, kind, ev.id, ev.title, ev.calendar, ev.owned_key))
        d += timedelta(days=1)
    return out


@dataclass
class Calendar:
    """The classified calendar for a horizon."""
    lo: date
    hi: date
    tz: ZoneInfo
    days: dict[date, list[Segment]] = field(default_factory=dict)
    owned: list[tuple[Event, str]] = field(default_factory=list)   # (event, kind="owned")
    instances: list[tuple[Event, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def segments(self, day: date) -> list[Segment]:
        return self.days.get(day, [])


def classify_calendar(events: list[Event], rules: list[Any], *, lo: date, hi: date, tz: ZoneInfo,
                      key_prefix: str = "dk_") -> Calendar:
    exp = expand(events, lo, hi, tz)
    clf = Classifier(rules, key_prefix)
    cal = Calendar(lo=lo, hi=hi, tz=tz)
    by_kind: dict[str, int] = {}
    for ev in exp.events:
        kind = clf.kind(ev)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        cal.instances.append((ev, kind))
        if kind == "owned":
            cal.owned.append((ev, kind))
            continue  # owned blocks are the scheduler's overlay, not fixed availability
        for seg in clip(ev, kind, lo, hi, tz):
            cal.days.setdefault(seg.day, []).append(seg)
    for segs in cal.days.values():
        segs.sort(key=lambda s: (s.start, s.end, s.event_id))
    cal.counts = {"considered": exp.considered, "masters": exp.masters, "exceptions": exp.exceptions,
                  "singles": exp.singles, "ignored": exp.ignored, "generated": exp.generated,
                  "tombstones": exp.tombstones, "concrete": len(exp.events),
                  **{f"kind.{k}": v for k, v in sorted(by_kind.items())}}
    return cal
