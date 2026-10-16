"""The plan-side data model (TD §12), independent of any calendar adapter.

- ``Event``: one calendar event as a provider returns it (a single event, a
  recurring master with ``recurrence``, an instance of a series, or a
  cancelled tombstone).
- ``Segment``: one classified piece of an event on one local day, in minutes.
- ``Task``: one schedulable unit of work (a backlog task, an answered task,
  or a daily batch of small tasks).
- ``Policy``: the ``planning.policy`` section resolved to minutes and dates.
- ``PlanWindow``: a scheduling window resolved to per-day minute ranges.
- ``Block``: a placed or existing block on one day.
- ``CalendarProvider``: the protocol a calendar adapter implements so the
  planner can read availability. M6 adapters plug in here.

Times inside the planner are local wall-clock minutes of a day (0..1440) in
the profile timezone. Conversion to and from instants happens only at the
edges (providers, operations).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from docket.config.digest import digest

DAY = 1440
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_clock_range(text: str) -> tuple[int, int]:
    """``"09:00-17:00"`` -> (540, 1020). ``24:00`` is allowed as an end. Raises ValueError."""
    m = _CLOCK.match(str(text))
    if not m:
        raise ValueError(text)
    h0, m0, h1, m1 = (int(x) for x in m.groups())
    a, b = h0 * 60 + m0, h1 * 60 + m1
    if m0 > 59 or m1 > 59 or not (0 <= a < b <= DAY):
        raise ValueError(text)
    return a, b


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def at(day: date, minutes: int, tz: ZoneInfo) -> datetime:
    """The aware instant of ``minutes`` past local midnight of ``day`` (1440 is the next midnight)."""
    if minutes >= DAY:
        return datetime.combine(day + timedelta(days=minutes // DAY), time(0), tzinfo=tz) + \
            timedelta(minutes=minutes % DAY)
    return datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=tz)


def minute_of(dt: datetime, tz: ZoneInfo) -> tuple[date, int]:
    local = dt.astimezone(tz)
    return local.date(), local.hour * 60 + local.minute


# --- events ------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """A calendar event. ``start``/``end`` are aware datetimes, or dates when ``all_day``
    (end exclusive). ``status`` is ``confirmed``, ``tentative`` or ``cancelled`` (a tombstone)."""
    id: str
    calendar: str
    title: str
    start: datetime | date
    end: datetime | date
    all_day: bool = False
    transparency: str = "opaque"
    properties: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    owned_key: str | None = None
    recurring: bool = False
    status: str = "confirmed"
    recurrence: dict[str, Any] | None = field(default=None, hash=False, compare=False)
    series_id: str | None = None          # for an instance or exception of a series
    original_start: datetime | date | None = None
    notes: str = ""

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id, "calendar": self.calendar, "title": self.title,
            "start": self.start.isoformat(), "end": self.end.isoformat(), "all_day": self.all_day,
            "transparency": self.transparency, "status": self.status,
        }
        for k in ("owned_key", "series_id", "recurrence"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.original_start is not None:
            d["original_start"] = self.original_start.isoformat()
        if self.recurring:
            d["recurring"] = True
        if self.properties:
            d["properties"] = self.properties
        if self.notes:
            d["notes"] = self.notes
        return d


@dataclass(frozen=True)
class Segment:
    """One classified piece of an event on one day, in local minutes."""
    day: date
    start: int
    end: int
    kind: str
    event_id: str
    title: str = ""
    calendar: str = ""
    owned_key: str | None = None


class CalendarProvider(Protocol):
    """Reads availability. ``list_events`` returns every event touching ``[start, end)``,
    including recurring masters (expanded by the planner within the horizon only) and
    cancelled tombstones (ignored by the planner)."""

    def list_events(self, start: datetime, end: datetime) -> list[Event]: ...


# --- policy ------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    workdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    day_window: tuple[int, int] = (540, 1020)
    deep_window: tuple[int, int] | None = None
    deep_kinds: frozenset[str] = frozenset({"deep"})
    deep_min: int = 60
    lunch: tuple[int, int] | None = None
    granularity: int = 15
    min_block: int = 30
    max_block: int = 120
    meeting_buffer: int = 0
    daily_cap: int | None = None
    horizon_days: int = 14
    move_cap: int = 3
    move_lock: int = 0
    ordering: tuple[str, ...] = ("imminent", "overdue", "due", "tier", "size")
    imminent_days: int = 2
    default_lead_days: int = 3
    default_est: int = 30
    busy_kinds: frozenset[str] = frozenset({"meeting", "focus", "soft_hold", "out_of_office", "boundary",
                                            "mirrored", "owned"})
    window_available_kinds: frozenset[str] = frozenset({"soft_hold", "out_of_office"})
    buffer_kinds: frozenset[str] = frozenset({"meeting"})
    batch_max: int = 15
    batch_block_max: int = 60
    key_prefix: str = "dk_"

    @classmethod
    def from_config(cls, planning: Any, profile_workdays: list[str] | None = None) -> "Policy":
        from docket.text.durations import parse_duration
        p = planning.policy
        days = p.workdays if p.workdays is not None else (profile_workdays or list(WEEKDAYS[:5]))
        dur = parse_duration
        return cls(
            workdays=frozenset(WEEKDAYS.index(d) for d in days),
            day_window=parse_clock_range(p.day_window),
            deep_window=parse_clock_range(p.deep_window) if p.deep_window else None,
            deep_kinds=frozenset(p.deep_kinds), deep_min=dur(p.deep_min),
            lunch=parse_clock_range(p.lunch) if p.lunch else None,
            granularity=max(1, dur(p.granularity)), min_block=dur(p.min_block), max_block=dur(p.max_block),
            meeting_buffer=dur(p.meeting_buffer),
            daily_cap=dur(p.daily_cap) if p.daily_cap is not None else None,
            horizon_days=p.horizon_days, move_cap=p.move_cap, move_lock=dur(p.move_lock),
            ordering=tuple(p.ordering), imminent_days=p.imminent_days, default_lead_days=p.default_lead_days,
            default_est=dur(p.default_est), busy_kinds=frozenset(p.busy_kinds),
            window_available_kinds=frozenset(p.window_available_kinds), buffer_kinds=frozenset(p.buffer_kinds),
            batch_max=dur(p.batch.max_minutes), batch_block_max=dur(p.batch.block_max),
            key_prefix=planning.key_prefix,
        )

    def round_up(self, minutes: int) -> int:
        g = self.granularity
        return ((minutes + g - 1) // g) * g

    def length_for(self, task: "Task") -> int:
        """The block length a task needs: its estimate on the grid, never below the minimum block."""
        n = max(self.round_up(task.est), self.round_up(self.min_block))
        if task.kind == "batch":
            n = min(n, max(self.round_up(self.min_block), self.batch_block_max))
        return n

    def block_kind(self, task: "Task") -> str:
        if task.kind:
            return task.kind
        return "deep" if task.est >= self.deep_min else "shallow"


# --- windows -----------------------------------------------------------------


@dataclass(frozen=True)
class PlanWindow:
    """A window resolved to per-day ranges. ``explicit_times``: the ranges replace the day
    window, lunch and the deep window. ``explicit_days``: named days replace the workday rule."""
    label: str
    segments: tuple[tuple[date, int, int], ...]
    explicit_times: bool = False
    explicit_days: bool = False
    event_id: str | None = None

    def days(self) -> list[date]:
        return sorted({d for d, _, _ in self.segments})

    def bounds(self, day: date) -> list[tuple[int, int]]:
        return [(s, e) for d, s, e in self.segments if d == day]

    def contains(self, day: date, start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e in self.bounds(day))


# --- tasks and blocks --------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """One schedulable unit. ``kind`` is None for a plain task (deep or shallow by length)
    or ``batch`` for a daily batch whose ``members`` are task ids."""
    id: str
    title: str
    est: int
    due: date | None = None
    earliest: date | None = None
    tier: int = 0
    size: str | None = None
    window: PlanWindow | None = None
    window_text: str | None = None
    carry: int = 0
    deadline: bool = False
    imminent: bool = False
    overdue: bool = False
    kind: str | None = None
    members: tuple[str, ...] = ()
    source: str = "store"
    question_id: str | None = None
    defaulted_est: bool = False

    def with_flags(self, today: date, imminent_days: int) -> "Task":
        overdue = self.due is not None and self.due < today
        imminent = self.due is not None and not overdue and (self.due - today).days <= imminent_days
        return replace(self, overdue=overdue, imminent=imminent)


def task_key(task: Task, prefix: str) -> str:
    """The ownership key of a task's block: stable across runs for the same task id."""
    if task.kind == "batch":
        return f"{prefix}b_{digest(['batch', task.id])[:12]}"
    return f"{prefix}t_{digest(['task', task.id])[:12]}"


def mirror_key(prefix: str, to_calendar: str, source_key: str) -> str:
    return f"{prefix}m_{digest(['mirror', to_calendar, source_key])[:12]}"


@dataclass(frozen=True)
class Block:
    key: str
    kind: str                 # deep | shallow | batch
    day: date
    start: int
    end: int
    task_ids: tuple[str, ...] = ()
    title: str = ""
    notes: str = ""
    window: PlanWindow | None = None
    earliest: date | None = None
    locked: bool = False

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str = ""


@dataclass(frozen=True)
class Placement:
    block: Block
    late: bool = False


@dataclass(frozen=True)
class Unschedulable:
    task_id: str
    reason: str
    detail: str = ""
