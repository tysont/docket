"""Calendar providers for the planner.

``InMemoryProvider`` serves a list of events (tests, sandbox twins).
``ListingProvider`` reads the calendar destination itself (``planning.calendar.provider:
destination``), so the blocks apply wrote are the blocks the next run plans around.
``JsonFileProvider`` reads a JSON fixture file:

```json
{"timezone": "America/Chicago",
 "events": [
   {"id": "e1", "calendar": "work", "title": "Team sync", "start": "2026-10-15T10:00:00-05:00",
    "end": "2026-10-15T10:30:00-05:00", "attendees": 4,
    "recurrence": {"freq": "weekly", "byday": ["thu"], "until": "2026-12-31"}},
   {"id": "e1-x", "series_id": "e1", "original_start": "2026-10-22T10:00:00-05:00", "status": "cancelled"},
   {"id": "e2", "title": "Away", "all_day": true, "start": "2026-10-20", "end": "2026-10-21"}
 ]}
```

Fields Docket does not model go to ``Event.properties``, where classification
rules can match them. Naive times are read in the file's ``timezone`` (else
the profile's). The ownership key comes from ``owned_key`` or the configured
``key_property`` (a property of the fixture format), else from the ``owner``
reader: the calendar destination's ownership marker over ``notes``
(``docket.destinations.calendar.owned_key``, the one implementation of
ownership). A malformed event is skipped and recorded as an unexpected shape.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from docket.config.digest import digest
from docket.plan.types import Event
from docket.shapes.unexpected import unexpected

_KNOWN = {"id", "calendar", "title", "summary", "start", "end", "all_day", "transparency", "status", "owned_key",
          "recurrence", "series_id", "original_start", "notes", "recurring", "properties"}


class InMemoryProvider:
    def __init__(self, events: list[Event]) -> None:
        self.events = list(events)

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        return list(self.events)


def _when(v: Any, tz: ZoneInfo, all_day: bool) -> datetime | date:
    s = str(v)
    if all_day:
        return date.fromisoformat(s[:10])
    if len(s) == 10:
        return datetime.combine(date.fromisoformat(s), time(0), tzinfo=tz)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


# (notes, attendees, recurring) -> the ownership key, or None when Docket does not own the event
Owner = Callable[[str, int, bool], "str | None"]


def event_from_json(row: dict[str, Any], tz: ZoneInfo, *, default_calendar: str = "work",
                    key_property: str = "docket_key", owner: Owner | None = None,
                    strip: Callable[[str], str] | None = None) -> Event:
    """Raises KeyError/ValueError/TypeError on a malformed row."""
    status = str(row.get("status") or "confirmed").lower()
    all_day = bool(row.get("all_day"))
    props = dict(row.get("properties") or {})
    props.update({k: v for k, v in row.items() if k not in _KNOWN})
    notes = str(row.get("notes") or "")
    recurring = bool(row.get("recurrence") or row.get("series_id") or row.get("recurring"))
    key = row.get("owned_key") or props.get(key_property)
    if not key and owner is not None and notes:
        att = props.get("attendees")
        n_att = len(att) if isinstance(att, list) else (int(att) if isinstance(att, int) else 0)
        key = owner(notes, n_att, recurring)
        if key and strip is not None:
            notes = strip(notes)  # the marker is ownership, not content: compare notes without it
    cancelled = status == "cancelled"
    start = _when(row["start"], tz, all_day) if "start" in row or not cancelled else None
    end = _when(row["end"], tz, all_day) if "end" in row or not cancelled else None
    orig = row.get("original_start")
    original = _when(orig, tz, all_day) if orig else None
    if start is None:  # a bare tombstone: it only cancels an occurrence
        start = end = original or datetime(1970, 1, 1, tzinfo=tz)
    return Event(
        id=str(row["id"]), calendar=str(row.get("calendar") or default_calendar),
        title=str(row.get("title") if row.get("title") is not None else row.get("summary") or ""),
        start=start, end=end, all_day=all_day,  # type: ignore[arg-type]
        transparency=str(row.get("transparency") or "opaque").lower(), properties=props,
        owned_key=str(key) if key else None, recurring=recurring,
        status=status, recurrence=row.get("recurrence") or None,
        series_id=str(row["series_id"]) if row.get("series_id") else None, original_start=original, notes=notes,
    )


class JsonFileProvider:
    def __init__(self, path: Path, tz: ZoneInfo, *, default_calendar: str = "work", key_property: str = "docket_key",
                 owner: Owner | None = None, strip: Callable[[str], str] | None = None) -> None:
        self.path = path
        self.strip = strip
        self.tz = tz
        self.default_calendar = default_calendar
        self.key_property = key_property
        self.owner = owner
        self._raw: dict[str, Any] | None = None

    def raw(self) -> dict[str, Any]:
        if self._raw is None:
            if not self.path.is_file():
                unexpected("plan.calendar", "calendar file missing", reading="plan with an empty calendar",
                           extend="planning.calendar.path")
                self._raw = {"events": []}
            else:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except ValueError:
                    unexpected("plan.calendar", "calendar file is not JSON", reading="plan with an empty calendar",
                               extend="planning.calendar.path")
                    data = {"events": []}
                self._raw = data if isinstance(data, dict) else {"events": data if isinstance(data, list) else []}
        return self._raw

    def digest(self) -> str:
        return digest(self.raw())

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        raw = self.raw()
        tz = self.tz
        if raw.get("timezone"):
            try:
                tz = ZoneInfo(str(raw["timezone"]))
            except Exception:
                unexpected("plan.calendar", "unknown fixture timezone", reading="use the profile timezone",
                           extend="planning.calendar.path: timezone")
        out = []
        for row in raw.get("events") or []:
            if not isinstance(row, dict):
                unexpected("plan.calendar", "event is not an object", reading="skip the event",
                           extend="planning.calendar.path")
                continue
            try:
                out.append(event_from_json(row, tz, default_calendar=self.default_calendar,
                                           key_property=self.key_property, owner=self.owner,
                                           strip=self.strip))
            except (KeyError, ValueError, TypeError) as e:
                unexpected("plan.calendar", f"malformed event: {type(e).__name__}", sample={"fields": sorted(row)},
                           reading="skip the event", extend="docket/plan/providers.py: event_from_json")
        return out


def _instant(value: str, tz: ZoneInfo) -> datetime | date:
    v = str(value).strip()
    if len(v) == 10:
        return date.fromisoformat(v)
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


class ListingProvider:
    """Events read from a calendar destination's backend.

    ``list_raw(window)`` returns the destination's events (``docket.destinations.calendar.Event``)
    and ``owner(event)`` its ownership key, both supplied by the engine so the planner never
    imports an adapter. ``window`` is the span ``digest()`` covers.
    """

    def __init__(self, list_raw: Callable[[tuple[str, str]], list[Any]], owner: Callable[[Any], "str | None"],
                 tz: ZoneInfo, *, window: tuple[datetime, datetime], default_calendar: str = "work",
                 where: str = "planning.calendar.destination", strip: Callable[[str], str] | None = None) -> None:
        self.list_raw = list_raw
        self.strip = strip
        self.owner = owner
        self.tz = tz
        self.window = window
        self.default_calendar = default_calendar
        self.where = where
        self._cache: dict[tuple[str, str], list[Any]] = {}

    def _raw(self, start: datetime, end: datetime) -> list[Any]:
        w = (start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
             end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if w not in self._cache:
            self._cache[w] = sorted(self.list_raw(w), key=lambda e: (e.start, e.id))
        return self._cache[w]

    def digest(self) -> str:
        return digest([e.as_dict() for e in self._raw(*self.window)])

    def list_events(self, start: datetime | None, end: datetime | None) -> list[Event]:
        rows = self._raw(start or self.window[0], end or self.window[1])
        out = []
        for e in rows:
            try:
                s, t = _instant(e.start, self.tz), _instant(e.end, self.tz)
            except (TypeError, ValueError):
                unexpected("plan.calendar", "destination event with an unreadable time", sample={"id": e.id},
                           reading="skip the event", extend=f"{self.where}: the adapter's event times")
                continue
            all_day = isinstance(s, date) and not isinstance(s, datetime)
            props = {"attendees": e.attendees} if e.attendees else {}
            key = self.owner(e)
            notes = self.strip(e.notes) if key and self.strip is not None else e.notes
            out.append(Event(id=e.id, calendar=e.calendar or self.default_calendar, title=e.title, start=s, end=t,
                             all_day=all_day, transparency="transparent" if e.transparent else "opaque",
                             properties=props, owned_key=key, recurring=e.recurring, notes=notes))
        return out
