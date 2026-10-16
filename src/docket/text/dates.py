"""The only parser for dates, weekdays and windows (TD §15.5).

Weekday names resolve to the next occurrence strictly after the reference
date. ``today``, ``tomorrow`` and ISO dates are taken as given. Windows are a
day with an optional time range (``thu 13:00-17:00``, ``2026-10-22 9-11``) or
``this week``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from docket.cli.errors import InputError

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_date(text: str, ref: date) -> date:
    """Resolve a date expression against ``ref``. Raises ``input.bad_date``."""
    s = str(text).strip().lower()
    if s == "today":
        return ref
    if s == "tomorrow":
        return ref + timedelta(days=1)
    if s.startswith("next "):
        s = s[5:].strip()
    if s in _WEEKDAY_NAMES:
        delta = (_WEEKDAY_NAMES[s] - ref.weekday()) % 7 or 7
        return ref + timedelta(days=delta)
    if _ISO.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
    raise InputError(
        "input.bad_date",
        f"Not a date: {text!r}",
        hint="Use today, tomorrow, a weekday name or YYYY-MM-DD",
        details={"received": str(text)},
    )


def normalize_date(text: str, ref: date) -> str:
    return parse_date(text, ref).isoformat()


def _parse_time(text: str) -> time:
    m = _TIME.match(text.strip().lower())
    if not m:
        raise ValueError(text)
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return time(h, mi)


@dataclass(frozen=True)
class Window:
    start_date: date
    end_date: date
    start: time | None = None
    end: time | None = None

    def normalized(self) -> str:
        if self.start_date != self.end_date:
            return f"{self.start_date.isoformat()}..{self.end_date.isoformat()}"
        s = self.start_date.isoformat()
        if self.start and self.end:
            s += f" {self.start:%H:%M}-{self.end:%H:%M}"
        return s


def parse_window(text: str, ref: date) -> Window:
    """Parse a window. Raises ``input.bad_window``."""
    s = " ".join(str(text).strip().lower().split())
    bad = InputError(
        "input.bad_window",
        f"Not a window: {text!r}",
        hint="Use a day (thu, 2026-10-22), a day with times (thu 13:00-17:00) or 'this week'",
        details={"received": str(text)},
    )
    if s == "this week":
        end = ref + timedelta(days=(6 - ref.weekday()))
        return Window(ref, end)
    parts = s.split(" ", 1)
    try:
        day = parse_date(parts[0], ref)
    except InputError:
        raise bad from None
    if len(parts) == 1:
        return Window(day, day)
    rng = parts[1].replace(" ", "")
    if "-" not in rng:
        raise bad
    a, b = rng.split("-", 1)
    try:
        t0, t1 = _parse_time(a), _parse_time(b)
    except ValueError:
        raise bad from None
    if t1 <= t0:
        raise bad
    return Window(day, day, t0, t1)


def parse_instant(text: str) -> datetime:
    """Parse an ISO instant. Naive values are rejected: a pinned clock needs an offset."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise InputError("input.bad_date", f"Not an ISO instant: {text!r}",
                         hint="Use YYYY-MM-DDTHH:MM:SS+HH:MM") from None
    if dt.tzinfo is None:
        raise InputError("input.bad_date", f"Instant has no UTC offset: {text!r}",
                         hint="Add an offset, for example 2026-10-15T18:00:00-05:00")
    return dt
