"""The pinned run clock (TD §4.6).

Dates, windows and the run's day come from the pinned clock, so derived output is
reproducible. One thing reads the wall clock: a live plan, which places nothing before the
moment it is built (``live_now``). A run whose clock was given (``run start --now``, used by
tests and replays) never reads it. ``wall_now`` is the one seam to the wall clock; tests
replace it instead of sleeping.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from docket.store.runs import Clock
from docket.text.dates import parse_date, parse_instant


def wall_now() -> datetime:
    """The wall clock. Every read of real time by a run goes through here."""
    return datetime.now(timezone.utc)


def pin(tz_name: str, *, now: str | None = None, on_date: str | None = None) -> Clock:
    """Pin a clock. ``now`` (ISO with offset) gives the instant instead of the wall clock, and
    the clock records that it was given; ``on_date`` the run date."""
    tz = ZoneInfo(tz_name)
    instant = parse_instant(now) if now else wall_now()
    local = instant.astimezone(tz)
    day = parse_date(on_date, local.date()) if on_date else local.date()
    return Clock(date=day.isoformat(), timezone=tz_name, instant=local.isoformat(), given=now is not None)


def pinned_now(clock: Clock) -> datetime:
    return datetime.fromisoformat(clock.instant).astimezone(ZoneInfo(clock.timezone))


def live_now(clock: Clock) -> datetime:
    """The moment a plan is built for: the later of the pinned instant and the wall clock, or
    the pinned instant alone when the clock was given."""
    pinned = pinned_now(clock)
    if clock.given:
        return pinned
    return max(pinned, wall_now().astimezone(pinned.tzinfo))


def run_date(clock: Clock) -> date:
    return date.fromisoformat(clock.date)
