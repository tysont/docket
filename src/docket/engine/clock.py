"""The pinned run clock (TD §4.6). Nothing derived reads the wall clock."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from docket.store.runs import Clock
from docket.text.dates import parse_date, parse_instant


def pin(tz_name: str, *, now: str | None = None, on_date: str | None = None) -> Clock:
    """Pin a clock. ``now`` (ISO with offset) overrides the wall clock; ``on_date`` the run date."""
    tz = ZoneInfo(tz_name)
    instant = parse_instant(now) if now else datetime.now(timezone.utc)
    local = instant.astimezone(tz)
    day = parse_date(on_date, local.date()) if on_date else local.date()
    return Clock(date=day.isoformat(), timezone=tz_name, instant=local.isoformat())


def run_date(clock: Clock) -> date:
    return date.fromisoformat(clock.date)
