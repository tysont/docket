"""Builders for planning tests. All data is invented."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from docket.config.models import ClassifyRule
from docket.plan.calendar_classify import Calendar, classify_calendar
from docket.plan.constraints import Ctx
from docket.plan.types import Event, Policy, Task

TZ = ZoneInfo("America/Chicago")
TODAY = date(2026, 10, 15)          # a Thursday
NOW = datetime(2026, 10, 15, 8, 0, tzinfo=TZ)


def d(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def dt(day: date, clock: str) -> datetime:
    return datetime.combine(day, time(*map(int, clock.split(":"))), tzinfo=TZ)


def ev(id: str, day: date, start: str, end: str, title: str = "Team sync", *, calendar: str = "work",
       attendees: int = 3, end_day: date | None = None, **kw: Any) -> Event:
    props = dict(kw.pop("properties", {}))
    if attendees:
        props["attendees"] = attendees
    return Event(id=id, calendar=calendar, title=title, start=dt(day, start), end=dt(end_day or day, end),
                 properties=props, **kw)


def allday(id: str, day: date, days: int = 1, title: str = "Away OOO", **kw: Any) -> Event:
    return Event(id=id, calendar=kw.pop("calendar", "work"), title=title, start=day, end=day + timedelta(days=days),
                 all_day=True, **kw)


def owned(key: str, day: date, start: str, end: str, title: str = "Block", **kw: Any) -> Event:
    return Event(id="o_" + key + start, calendar="work", title=title, start=dt(day, start), end=dt(day, end),
                 owned_key=key, **kw)


def rule(kind: str, **match: Any) -> ClassifyRule:
    return ClassifyRule.model_validate({"match": match, "kind": kind})


RULES = [
    rule("focus", title_regex="^Focus"),
    rule("out_of_office", title_regex=r"\bOOO\b"),
    rule("soft_hold", title_regex="^Hold"),
    rule("boundary", title_regex="^Commute"),
    rule("mirrored", calendar="personal"),
    rule("informational", transparency="transparent"),
    rule("meeting", property={"attendees": True}),
    rule("informational", all_day=True),
]


def policy(**kw: Any) -> Policy:
    base = Policy(workdays=frozenset({0, 1, 2, 3, 4}), day_window=(540, 1020), granularity=15, min_block=30,
                  max_block=120, horizon_days=14, move_cap=3, batch_max=15, batch_block_max=60)
    return replace(base, **kw)


def calendar(events: list[Event], *, rules: list[ClassifyRule] | None = None, horizon: int = 14) -> Calendar:
    return classify_calendar(events, RULES if rules is None else rules, lo=TODAY, hi=TODAY + timedelta(days=horizon),
                             tz=TZ)


def ctx(events: list[Event] | None = None, *, pol: Policy | None = None, now: datetime = NOW) -> Ctx:
    p = pol or policy()
    return Ctx(p, calendar(events or [], horizon=p.horizon_days), TODAY, now, TZ)


def task(id: str, est: int = 60, **kw: Any) -> Task:
    return Task(id=id, title=kw.pop("title", f"Task {id}"), est=est, **kw)
