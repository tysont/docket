"""The one constraint engine (TD §12.2). The only definition of placement legality.

- ``free_intervals(day, policy, events, window=, overlay=)``: the stretches of a
  day a block may use, before the per-block constraints.
- ``check(block, ctx)``: every violated constraint of one block, in
  ``CONSTRAINTS`` order. An empty list means the block is legal.
- ``search(task, ctx)``: the earliest legal placement, or ``Unschedulable``
  carrying the tightest violated constraint.

The scheduler places blocks with ``search`` and the validator calls ``check``
and ``search`` again, so a built plan always validates and an unschedulable
claim is reproducible (FR-PLN-05). ``free_intervals`` only proposes
candidates; every candidate is confirmed by ``check``.

``Ctx`` keeps the classified calendar per day and an overlay of placed blocks.
Free intervals are computed once per day and updated incrementally on each
placement (NFR-06).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from docket.plan.calendar_classify import Calendar
from docket.plan.types import (
    DAY, Block, Placement, PlanWindow, Policy, Segment, Task, Unschedulable, Violation, hhmm, task_key,
)

# Constraint codes in reporting order: structural first, then the calendar.
CONSTRAINTS = (
    "horizon", "workday", "past", "earliest", "window", "day_window", "granularity", "min_block", "max_block",
    "deep_window", "lunch", "overlap", "buffer", "daily_cap", "batch_per_day",
)
# Constraints that describe where a task may go at all; they never count as "tightest".
STRUCTURAL = frozenset({"horizon", "workday", "past", "earliest", "window", "day_window", "granularity"})

Interval = tuple[int, int]


def subtract(ivs: list[Interval], a: int, b: int) -> list[Interval]:
    if b <= a:
        return ivs
    out: list[Interval] = []
    for s, e in ivs:
        if e <= a or s >= b:
            out.append((s, e))
            continue
        if s < a:
            out.append((s, a))
        if e > b:
            out.append((b, e))
    return out


def _clip(ivs: list[Interval], lo: int, hi: int) -> list[Interval]:
    return [(max(s, lo), min(e, hi)) for s, e in ivs if min(e, hi) > max(s, lo)]


def _busy_for(seg: Segment, policy: Policy, window: PlanWindow | None) -> bool:
    if seg.kind not in policy.busy_kinds:
        return False
    return not (window is not None and seg.kind in policy.window_available_kinds)


def free_intervals(day: date, policy: Policy, events: list[Segment], *, window: PlanWindow | None,
                   overlay: Iterable[Block], now_min: int = 0) -> list[Interval]:
    """Stretches of ``day`` open to a block: inside the day window (or the window's own ranges
    when it gives times), on a workday unless the window names the day, outside lunch, busy
    events with their buffers, placed blocks, and the past."""
    if window is not None:
        bounds = window.bounds(day)
        if not bounds:
            return []
        if not window.explicit_times:
            bounds = _clip(bounds, *policy.day_window)
        if not window.explicit_days and day.weekday() not in policy.workdays:
            return []
    else:
        if day.weekday() not in policy.workdays:
            return []
        bounds = [policy.day_window]
    ivs = sorted(bounds)
    if policy.lunch and not (window is not None and window.explicit_times):
        ivs = subtract(ivs, *policy.lunch)
    for seg in events:
        if not _busy_for(seg, policy, window):
            continue
        pad = policy.meeting_buffer if seg.kind in policy.buffer_kinds else 0
        ivs = subtract(ivs, seg.start - pad, seg.end + pad)
    for b in overlay:
        ivs = subtract(ivs, b.start, b.end)
    if now_min:
        ivs = subtract(ivs, 0, now_min)
    return ivs


@dataclass
class Ctx:
    """Calendar, policy and the overlay of placed blocks for one planning pass."""
    policy: Policy
    cal: Calendar
    today: date
    now: datetime
    tz: ZoneInfo
    overlay: dict[date, dict[str, Block]] = field(default_factory=dict)
    _free: dict[date, list[Interval]] = field(default_factory=dict)
    _minutes: dict[date, int] = field(default_factory=dict)

    @property
    def horizon_end(self) -> date:
        return self.today + timedelta(days=self.policy.horizon_days)

    def days(self) -> list[date]:
        return [self.today + timedelta(days=i) for i in range(self.policy.horizon_days)]

    def now_min(self, day: date) -> int:
        local = self.now.astimezone(self.tz)
        if day < local.date():
            return DAY
        if day == local.date():
            return local.hour * 60 + local.minute + (1 if local.second or local.microsecond else 0)
        return 0

    # overlay -----------------------------------------------------------------
    def blocks(self, day: date) -> list[Block]:
        return list(self.overlay.get(day, {}).values())

    def all_blocks(self) -> list[Block]:
        return [b for d in sorted(self.overlay) for b in sorted(self.overlay[d].values(), key=lambda x: x.start)]

    def add(self, b: Block) -> None:
        self.overlay.setdefault(b.day, {})[b.key] = b
        self._minutes[b.day] = self._minutes.get(b.day, 0) + b.minutes
        if b.day in self._free:
            self._free[b.day] = subtract(self._free[b.day], b.start, b.end)

    def remove(self, b: Block) -> None:
        day = self.overlay.get(b.day, {})
        if day.pop(b.key, None) is not None:
            self._minutes[b.day] -= b.minutes
            self._free.pop(b.day, None)

    def minutes(self, day: date, *, excluding: str | None = None) -> int:
        n = self._minutes.get(day, 0)
        if excluding is not None:
            b = self.overlay.get(day, {}).get(excluding)
            if b is not None:
                n -= b.minutes
        return n

    def free(self, day: date, window: PlanWindow | None = None) -> list[Interval]:
        if window is None:
            if day not in self._free:
                self._free[day] = free_intervals(day, self.policy, self.cal.segments(day), window=None,
                                                 overlay=self.blocks(day), now_min=self.now_min(day))
            return self._free[day]
        return free_intervals(day, self.policy, self.cal.segments(day), window=window,
                              overlay=self.blocks(day), now_min=self.now_min(day))

    def copy(self, *, overlay: Iterable[Block] = ()) -> "Ctx":
        c = Ctx(self.policy, self.cal, self.today, self.now, self.tz)
        for b in overlay:
            c.add(b)
        return c


# --- check -----------------------------------------------------------------------


def _within(a: int, b: int, rng: tuple[int, int]) -> bool:
    return rng[0] <= a and b <= rng[1]


def _hits(a: int, b: int, s: int, e: int) -> bool:
    return a < e and s < b


def check(block: Block, ctx: Ctx) -> list[Violation]:
    """Every constraint ``block`` violates, in ``CONSTRAINTS`` order. The block's own key is
    excluded from the overlay, so an existing block can be checked in place."""
    p, day, a, b, w = ctx.policy, block.day, block.start, block.end, block.window
    out: list[Violation] = []
    if day < ctx.today or day >= ctx.horizon_end:
        out.append(Violation("horizon", f"{day} is outside {ctx.today}..{ctx.horizon_end - timedelta(days=1)}"))
    if day.weekday() not in p.workdays and not (w is not None and w.explicit_days and w.bounds(day)):
        out.append(Violation("workday", f"{day:%a} is not a workday"))
    if a < ctx.now_min(day):
        out.append(Violation("past", "starts before the run's pinned now"))
    if block.earliest is not None and day < block.earliest:
        out.append(Violation("earliest", f"before the earliest start {block.earliest}"))
    if w is not None and not w.contains(day, a, b):
        out.append(Violation("window", f"outside the window {w.label}"))
    explicit_times = w is not None and w.explicit_times
    if not explicit_times and not _within(a, b, p.day_window):
        out.append(Violation("day_window", f"outside the day window {hhmm(p.day_window[0])}-{hhmm(p.day_window[1])}"))
    if a % p.granularity or (b - a) % p.granularity:
        out.append(Violation("granularity", f"not on the {p.granularity}-minute grid"))
    if b - a < p.min_block:
        out.append(Violation("min_block", f"{b - a}m is under the minimum block {p.min_block}m"))
    if b - a > p.max_block:
        out.append(Violation("max_block", f"{b - a}m is over the maximum block {p.max_block}m"))
    if (block.kind in p.deep_kinds and p.deep_window and not explicit_times
            and not _within(a, b, p.deep_window)):
        out.append(Violation("deep_window", f"a {block.kind} block must sit inside "
                                            f"{hhmm(p.deep_window[0])}-{hhmm(p.deep_window[1])}"))
    if p.lunch and not explicit_times and _hits(a, b, *p.lunch):
        out.append(Violation("lunch", f"overlaps lunch {hhmm(p.lunch[0])}-{hhmm(p.lunch[1])}"))
    segs = ctx.cal.segments(day)
    hit = next((s for s in segs if _busy_for(s, p, w) and _hits(a, b, s.start, s.end)), None)
    other = next((o for o in ctx.overlay.get(day, {}).values() if o.key != block.key and _hits(a, b, o.start, o.end)),
                 None)
    if hit is not None:
        out.append(Violation("overlap", f"overlaps a {hit.kind} {hhmm(hit.start)}-{hhmm(hit.end)}"))
    elif other is not None:
        out.append(Violation("overlap", f"overlaps block {other.key} {hhmm(other.start)}-{hhmm(other.end)}"))
    if p.meeting_buffer:
        pad = p.meeting_buffer
        near = next((s for s in segs if s.kind in p.buffer_kinds and _busy_for(s, p, w)
                     and _hits(a, b, s.start - pad, s.end + pad) and not _hits(a, b, s.start, s.end)), None)
        if near is not None:
            out.append(Violation("buffer", f"within {pad}m of a {near.kind} at {hhmm(near.start)}"))
    if p.daily_cap is not None and ctx.minutes(day, excluding=block.key) + (b - a) > p.daily_cap:
        out.append(Violation("daily_cap", f"{day} would exceed the daily cap of {p.daily_cap}m"))
    if block.kind == "batch" and any(o.kind == "batch" and o.key != block.key
                                     for o in ctx.overlay.get(day, {}).values()):
        out.append(Violation("batch_per_day", f"{day} already has a batch block"))
    return out


# --- search ----------------------------------------------------------------------


def block_for(task: Task, ctx: Ctx, day: date, start: int, *, key: str | None = None) -> Block:
    p = ctx.policy
    n = p.length_for(task)
    return Block(key=key or task_key(task, p.key_prefix), kind=p.block_kind(task), day=day, start=start,
                 end=start + n, task_ids=task.members or (task.id,), title=task.title, window=task.window,
                 earliest=task.earliest)


def candidate_days(task: Task, ctx: Ctx) -> tuple[list[date], list[date]]:
    """(on-time days, late days) the search walks, in order."""
    if task.window is not None:
        days = [d for d in task.window.days() if ctx.today <= d < ctx.horizon_end]
    else:
        first = max(ctx.today, task.earliest or ctx.today)
        days = [d for d in ctx.days() if d >= first]
    if task.due is None:
        return days, []
    return [d for d in days if d <= task.due], [d for d in days if d > task.due]


def _grid_up(m: int, g: int) -> int:
    return ((m + g - 1) // g) * g


def search(task: Task, ctx: Ctx) -> Placement | Unschedulable:
    """The earliest legal slot on or after the task's earliest start, before its due date if
    possible, else the earliest legal slot in the horizon flagged late."""
    p = ctx.policy
    n = p.length_for(task)
    if n > p.max_block:
        return Unschedulable(task.id, "max_block", f"needs {n}m; the maximum block is {p.max_block}m")
    on_time, late = candidate_days(task, ctx)
    for days, is_late in ((on_time, False), (late, True)):
        for day in days:
            for s, e in ctx.free(day, task.window):
                t = _grid_up(s, p.granularity)
                while t + n <= e:
                    blk = block_for(task, ctx, day, t)
                    if not check(blk, ctx):
                        return Placement(blk, late=is_late)
                    t += p.granularity
    return tightest(task, ctx)


def slot_bounds(task: Task, ctx: Ctx, day: date) -> list[tuple[int, int]]:
    if task.window is not None:
        b = task.window.bounds(day)
        return b if task.window.explicit_times else _clip(b, *ctx.policy.day_window)
    return [ctx.policy.day_window]


def eligible_slots(task: Task, ctx: Ctx) -> Iterable[tuple[date, int]]:
    """Grid starts on eligible days (workdays or window days), inside the bounds, not past."""
    p = ctx.policy
    n = p.length_for(task)
    on_time, late = candidate_days(task, ctx)
    for day in on_time + late:
        named = task.window is not None and task.window.explicit_days
        if day.weekday() not in p.workdays and not named:
            continue
        floor = ctx.now_min(day)
        for lo, hi in slot_bounds(task, ctx, day):
            t = _grid_up(max(lo, floor), p.granularity)
            while t + n <= hi:
                yield day, t
                t += p.granularity


def tightest(task: Task, ctx: Ctx) -> Unschedulable:
    """Why no slot is legal: the constraint that is the only violation on the most eligible
    slots (relaxing it would open the most); else the most frequent first violation. Ties
    break by ``CONSTRAINTS`` order."""
    p = ctx.policy
    n = p.length_for(task)
    if n > p.max_block:
        return Unschedulable(task.id, "max_block", f"needs {n}m; the maximum block is {p.max_block}m")
    sole: dict[str, int] = {}
    first: dict[str, int] = {}
    slots = 0
    for day, t in eligible_slots(task, ctx):
        v = [x for x in check(block_for(task, ctx, day, t), ctx) if x.code not in STRUCTURAL]
        if not v:
            continue  # legal under the non-structural constraints; cannot happen after a failed search
        slots += 1
        first[v[0].code] = first.get(v[0].code, 0) + 1
        if len(v) == 1:
            sole[v[0].code] = sole.get(v[0].code, 0) + 1
    if slots == 0:
        if task.window is not None:
            return Unschedulable(task.id, "window", f"no eligible slot inside {task.window.label}")
        on_time, late = candidate_days(task, ctx)
        if not on_time + late:
            return Unschedulable(task.id, "horizon", "no day of the horizon is on or after the earliest start")
        return Unschedulable(task.id, "workday", "no workday with room for the block remains in the horizon")
    pool = sole or first
    order = {c: i for i, c in enumerate(CONSTRAINTS)}
    code = min(pool, key=lambda c: (-pool[c], order[c]))
    return Unschedulable(task.id, code, f"{code} blocks {pool[code]} of {slots} eligible slot(s)")
