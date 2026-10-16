"""Calendar providers for the planner.

``InMemoryProvider`` serves a list of events (tests, sandbox twins).
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
the profile's). The ownership key comes from ``owned_key``, the configured
``key_property``, or a ``marker_regex`` over ``notes``. A malformed event is
skipped and recorded as an unexpected shape.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
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


def event_from_json(row: dict[str, Any], tz: ZoneInfo, *, default_calendar: str = "work",
                    key_property: str = "docket_key", marker: re.Pattern[str] | None = None) -> Event:
    """Raises KeyError/ValueError/TypeError on a malformed row."""
    status = str(row.get("status") or "confirmed").lower()
    all_day = bool(row.get("all_day"))
    props = dict(row.get("properties") or {})
    props.update({k: v for k, v in row.items() if k not in _KNOWN})
    notes = str(row.get("notes") or "")
    key = row.get("owned_key") or props.get(key_property)
    if not key and marker is not None and notes:
        m = marker.search(notes)
        key = m.group(1) if m and m.groups() else None
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
        owned_key=str(key) if key else None, recurring=bool(row.get("recurrence") or row.get("series_id")
                                                             or row.get("recurring")),
        status=status, recurrence=row.get("recurrence") or None,
        series_id=str(row["series_id"]) if row.get("series_id") else None, original_start=original, notes=notes,
    )


class JsonFileProvider:
    def __init__(self, path: Path, tz: ZoneInfo, *, default_calendar: str = "work", key_property: str = "docket_key",
                 marker_regex: str | None = None) -> None:
        self.path = path
        self.tz = tz
        self.default_calendar = default_calendar
        self.key_property = key_property
        self.marker = re.compile(marker_regex) if marker_regex else None
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
                                           key_property=self.key_property, marker=self.marker))
            except (KeyError, ValueError, TypeError) as e:
                unexpected("plan.calendar", f"malformed event: {type(e).__name__}", sample={"fields": sorted(row)},
                           reading="skip the event", extend="docket/plan/providers.py: event_from_json")
        return out
