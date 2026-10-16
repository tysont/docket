"""The scheduler (TD §12.3): deterministic greedy placement with bounded repair.

1. Order tasks: window tasks first (an explicit operator instruction), then by
   the policy's ordering keys (imminent, overdue, due, tier, size by default),
   then id.
2. Batch tasks at or under ``batch.max_minutes`` (without a window or a later
   earliest start) into one daily batch per workday, filling in order.
3. Keep existing owned blocks: locked ones (elapsed, started, or inside the
   move lock) as they are; others while ``check`` passes and the length still
   matches. A kept block whose title or notes changed is an ``update``.
4. Place every remaining unit with ``search``. A unit with an existing block
   that moves is a ``move``; a new one is a ``create``.
5. Repair: an unschedulable unit with a due date may displace at most
   ``move_cap`` lower-priority, unlocked blocks if that frees a legal slot and
   every displaced block re-places. ``move_cap`` bounds displacements per run.
6. Retry the unschedulable until nothing changes, then record each one's
   reason from ``search`` against the final state, so the validator reproduces
   it exactly.
7. Release owned blocks with no open task, duplicates, and blocks of
   unschedulable units. Every owned input block gets exactly one operation.

Mirror holds are planned by ``plan/mirrors.py`` and arrive here as fixed
segments; the scheduler never touches them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from docket.plan.calendar_classify import Calendar
from docket.plan.constraints import Ctx, block_for, check, search, slot_bounds, candidate_days, _grid_up
from docket.plan.types import (
    Block, Event, Placement, Policy, Task, Unschedulable, at, minute_of, task_key,
)
from docket.plan.windows import window_free_minutes

Titler = Callable[[Task], tuple[str, str]]


def ordering_key(task: Task, policy: Policy) -> tuple[Any, ...]:
    parts: list[Any] = [0 if task.window is not None else 1]
    for k in policy.ordering:
        if k == "imminent":
            parts.append(0 if task.imminent else 1)
        elif k == "overdue":
            parts.append(0 if task.overdue else 1)
        elif k == "due":
            parts.append(task.due.isoformat() if task.due else "9999-12-31")
        elif k == "tier":
            parts.append(task.tier)
        elif k == "size":
            parts.append(task.est)
        elif k == "carry":
            parts.append(-task.carry)
    parts.append(task.id)
    return tuple(parts)


def describe_key(task: Task, policy: Policy) -> dict[str, Any]:
    out: dict[str, Any] = {"window": task.window.label if task.window else None}
    for k in policy.ordering:
        out[k] = {"imminent": task.imminent, "overdue": task.overdue, "due": task.due.isoformat() if task.due else None,
                  "tier": task.tier, "size": task.est, "carry": task.carry}[k]
    return out


@dataclass
class Existing:
    """An owned block found on the calendar."""
    key: str
    event: Event
    day: date
    start: int
    end: int

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass
class Unit:
    task: Task
    key: str
    rank: int
    existing: Existing | None = None
    block: Block | None = None
    late: bool = False
    locked: bool = False
    seq: int = -1
    reason: Unschedulable | None = None
    window_error: dict[str, Any] | None = None


@dataclass
class ScheduleResult:
    units: list[Unit]
    ops: list[dict[str, Any]]
    schedule: list[dict[str, Any]]
    unschedulable: list[dict[str, Any]]
    window_errors: list[dict[str, Any]]
    ctx: Ctx
    owned_keys: list[str]
    moves_used: int = 0
    notes: list[str] = field(default_factory=list)

    def unit_for(self, ref: str) -> Unit | None:
        for u in self.units:
            if ref in (u.task.id, u.key, u.task.question_id) or ref in u.task.members:
                return u
        return None


def prepare(tasks: list[Task], today: date, policy: Policy) -> list[Task]:
    return [t.with_flags(today, policy.imminent_days) for t in tasks]


def make_batches(ordered: list[Task], ctx: Ctx, titler: Titler | None) -> tuple[list[Task], list[Task]]:
    """(batch units, the tasks left to schedule individually)."""
    p = ctx.policy
    if p.batch_max <= 0:
        return [], ordered
    small = [t for t in ordered if t.est <= p.batch_max and t.window is None
             and (t.earliest is None or t.earliest <= ctx.today)]
    rest = [t for t in ordered if t not in small]
    days = [d for d in ctx.days() if d.weekday() in p.workdays]
    cap = max(p.round_up(p.min_block), p.batch_block_max)
    groups: list[list[Task]] = []
    cur: list[Task] = []
    total = 0
    for t in small:
        if cur and total + t.est > cap:
            groups.append(cur)
            cur, total = [], 0
        cur.append(t)
        total += t.est
    if cur:
        groups.append(cur)
    batches: list[Task] = []
    for i, members in enumerate(groups):
        if i >= len(days):
            rest.extend(members)  # more batches than workdays: the rest go one by one
            continue
        d = days[i]
        dues = [m.due for m in members if m.due]
        b = Task(id=f"batch:{d.isoformat()}", title=f"{len(members)} small tasks",
                 est=min(sum(m.est for m in members), cap), due=min(dues) if dues else None, earliest=d,
                 tier=min(m.tier for m in members), kind="batch", members=tuple(m.id for m in members),
                 imminent=any(m.imminent for m in members), overdue=any(m.overdue for m in members),
                 carry=max(m.carry for m in members))
        batches.append(b)
    return batches, rest


def _existing(cal: Calendar, tz: ZoneInfo, prefix: str, mirror_prefix: str) -> list[Existing]:
    out = []
    for ev, _ in cal.owned:
        k = ev.owned_key or ""
        if k.startswith(mirror_prefix):
            continue
        if ev.all_day or not isinstance(ev.start, datetime):
            d, s, e = (ev.start if not isinstance(ev.start, datetime) else ev.start.date()), 0, 1440
        else:
            d, s = minute_of(ev.start, tz)
            d2, e = minute_of(ev.end, tz)  # type: ignore[arg-type]
            e = e if d2 == d else 1440
        out.append(Existing(k, ev, d, s, e))
    out.sort(key=lambda x: (x.day, x.start, x.key, x.event.id))
    return out


def schedule(tasks: list[Task], cal: Calendar, policy: Policy, *, today: date, now: datetime, tz: ZoneInfo,
             calendar: str = "work", titler: Titler | None = None) -> ScheduleResult:
    ctx = Ctx(policy, cal, today, now, tz)
    prefix = policy.key_prefix
    tasks = prepare(tasks, today, policy)
    seen: set[str] = set()
    uniq = []
    for t in sorted(tasks, key=lambda t: ordering_key(t, policy)):
        if t.id not in seen:
            seen.add(t.id)
            uniq.append(t)
    batches, rest = make_batches(uniq, ctx, titler)
    all_units = sorted(batches + rest, key=lambda t: ordering_key(t, policy))
    if titler is not None:
        all_units = [replace(t, title=titler(t)[0]) for t in all_units]
    units = [Unit(t, task_key(t, prefix), i) for i, t in enumerate(all_units)]
    by_key = {u.key: u for u in units}

    existing = _existing(cal, tz, prefix, prefix + "m_")
    owned_keys = [x.key for x in existing]
    releases: list[tuple[Existing, str]] = []
    for x in existing:
        u = by_key.get(x.key)
        if u is None:
            releases.append((x, "no open task for this block"))
        elif u.existing is not None:
            releases.append((x, "duplicate block for the same task"))
        else:
            u.existing = x
    seq = 0

    def notes_of(t: Task) -> str:
        return titler(t)[1] if titler is not None else ""

    def place(u: Unit, blk: Block, late: bool = False) -> None:
        nonlocal seq
        blk = replace(blk, title=u.task.title, notes=notes_of(u.task))
        ctx.add(blk)
        u.block, u.late, u.seq = blk, late, seq
        seq += 1

    def existing_block(u: Unit) -> Block:
        x = u.existing
        assert x is not None
        b = block_for(u.task, ctx, x.day, x.start, key=u.key)
        return replace(b, end=x.end)

    # 3. keep: locked blocks first, then legal ones in order
    lock_until = now + timedelta(minutes=policy.move_lock)
    for u in units:
        x = u.existing
        if x is not None and at(x.day, x.start, tz) < lock_until:
            u.locked = True
            place(u, replace(existing_block(u), locked=True))
    for u in units:
        x = u.existing
        if x is None or u.block is not None:
            continue
        blk = existing_block(u)
        if x.minutes == policy.length_for(u.task) and not check(blk, ctx):
            place(u, blk)

    # window feasibility against the calendar alone (TD §12.4)
    for u in units:
        w = u.task.window
        if w is None or u.block is not None:
            continue
        base = ctx.copy()
        free = [iv for d in w.days() if today <= d < ctx.horizon_end for iv in base.free(d, w)]
        total, longest = window_free_minutes(free)
        need = policy.length_for(u.task)
        if longest < need:
            u.window_error = {"task_id": u.task.id, "question_id": u.task.question_id, "window": w.label,
                              "est": need, "free_minutes": total, "longest_free": longest}

    # 4. place, with 5. repair for a task that would otherwise be late or unschedulable
    pending: list[Unit] = []
    moves_used = 0
    for u in units:
        if u.block is not None or u.window_error is not None:
            continue
        res = search(u.task, ctx)
        if isinstance(res, Placement) and not res.late:
            place(u, res.block, False)
            continue
        if u.task.due is not None and moves_used < policy.move_cap:
            used = _repair(u, units, ctx, policy, policy.move_cap - moves_used, place)
            if used:
                moves_used += used
                continue
        if isinstance(res, Placement):
            place(u, res.block, True)
        else:
            pending.append(u)

    # 6. retry to a fixpoint, then record reasons against the final state
    changed = True
    while changed and pending:
        changed = False
        for u in list(pending):
            res = search(u.task, ctx)
            if isinstance(res, Placement):
                place(u, res.block, res.late)
                pending.remove(u)
                changed = True
    for u in pending:
        res = search(u.task, ctx)
        assert isinstance(res, Unschedulable)
        u.reason = res

    # 7. operations
    ops: list[dict[str, Any]] = []

    def span(day: date, s: int, e: int) -> tuple[str, str]:
        return at(day, s, tz).isoformat(), at(day, e, tz).isoformat()

    def base_op(verb: str, key: str, u: Unit | None, title: str, notes: str) -> dict[str, Any]:
        op = {"verb": verb, "calendar": calendar, "key": key, "title": title, "notes": notes,
              "kind": (u.block.kind if u and u.block else (u.task.kind or "task") if u else "task"),
              "task_ids": list(u.task.members or (u.task.id,)) if u else []}
        return op

    for u in units:
        x, b = u.existing, u.block
        if b is not None:
            st, en = span(b.day, b.start, b.end)
            if x is None:
                verb = "create"
            elif u.locked or (x.day, x.start, x.end) == (b.day, b.start, b.end):
                same = (x.event.title == b.title) and ((x.event.notes or "") == (b.notes or ""))
                verb = "keep" if (same or u.locked) else "update"
            else:
                verb = "move"
            op = base_op(verb, u.key, u, b.title, b.notes)
            op.update({"start": st, "end": en, "late": u.late})
            if u.locked:
                op["locked"] = True
            if verb == "move":
                fs, fe = span(x.day, x.start, x.end)  # type: ignore[union-attr]
                op["from"] = {"start": fs, "end": fe}
            ops.append(op)
        elif x is not None:
            reason = (f"unschedulable: {u.reason.reason}" if u.reason else
                      "window too small" if u.window_error else "not placed")
            releases.append((x, reason))
    for x, reason in releases:
        st, en = span(x.day, x.start, x.end)
        u = by_key.get(x.key)
        op = base_op("release", x.key, None, x.event.title, x.event.notes or "")
        if u is not None:
            op["task_ids"] = list(u.task.members or (u.task.id,))
        op.update({"start": st, "end": en, "reason": reason, "event_id": x.event.id})
        ops.append(op)
    ops.sort(key=lambda o: (o["start"], o["key"], o["verb"], o.get("event_id", "")))

    sched = []
    for u in sorted((u for u in units if u.block is not None), key=lambda u: (u.block.day, u.block.start, u.key)):
        b = u.block
        st, en = span(b.day, b.start, b.end)
        status = ("locked" if u.locked else "created" if u.existing is None else
                  "kept" if (u.existing.day, u.existing.start, u.existing.end) == (b.day, b.start, b.end) else "moved")
        sched.append({"key": u.key, "task_ids": list(u.task.members or (u.task.id,)), "title": b.title,
                      "kind": b.kind, "date": b.day.isoformat(), "start": st, "end": en, "minutes": b.minutes,
                      "status": status, "late": u.late, "seq": u.seq})
    unsched = [{"task_id": u.task.id, "title": u.task.title, "question_id": u.task.question_id,
                "members": list(u.task.members), "reason": u.reason.reason, "detail": u.reason.detail}
               for u in units if u.reason is not None]
    werr = [u.window_error for u in units if u.window_error is not None]
    return ScheduleResult(units, ops, sched, unsched, werr, ctx, owned_keys, moves_used)


def _repair(u: Unit, units: list[Unit], ctx: Ctx, policy: Policy, cap: int,
            place: Callable[[Unit, Block, bool], None]) -> int:
    """Displace at most ``cap`` lower-priority unlocked blocks so ``u`` fits. Returns the number
    displaced, or 0 with the state unchanged."""
    by_block = {x.block.key: x for x in units if x.block is not None}
    task = u.task
    n = policy.length_for(task)
    on_time, late = candidate_days(task, ctx)
    for day in on_time:
        for lo, hi in slot_bounds(task, ctx, day):
            t = _grid_up(max(lo, ctx.now_min(day)), policy.granularity)
            while t + n <= hi:
                conflicts = [b for b in ctx.blocks(day) if b.start < t + n and t < b.end]
                t0 = t
                t += policy.granularity
                if not conflicts or len(conflicts) > cap:
                    continue
                owners = [by_block.get(b.key) for b in conflicts]
                if any(o is None or o.locked or o.rank <= u.rank for o in owners):
                    continue
                saved = [(o, o.block, o.late, o.seq) for o in owners]  # type: ignore[union-attr]
                for o in owners:
                    ctx.remove(o.block)  # type: ignore[union-attr]
                    o.block = None  # type: ignore[union-attr]
                blk = block_for(task, ctx, day, t0)
                if check(blk, ctx):
                    for o, b, lt, sq in saved:
                        ctx.add(b)
                        o.block, o.late, o.seq = b, lt, sq
                    continue
                place(u, blk, False)
                ok = True
                placed: list[Unit] = []
                for o in sorted(owners, key=lambda x: x.rank):  # type: ignore[union-attr]
                    res = search(o.task, ctx)  # type: ignore[union-attr]
                    if isinstance(res, Placement):
                        place(o, res.block, res.late)  # type: ignore[arg-type]
                        placed.append(o)  # type: ignore[arg-type]
                    else:
                        ok = False
                        break
                if ok:
                    return len(conflicts)
                for o in placed:
                    ctx.remove(o.block)  # type: ignore[arg-type]
                ctx.remove(u.block)  # type: ignore[arg-type]
                u.block, u.seq = None, -1
                for o, b, lt, sq in saved:
                    ctx.add(b)
                    o.block, o.late, o.seq = b, lt, sq
    return 0


def validate(result: ScheduleResult) -> list[str]:
    """Re-check a built schedule with the same engine (FR-PLN-05, FR-PLN-06)."""
    problems: list[str] = []
    c = result.ctx.copy(overlay=[u.block for u in result.units if u.block is not None])
    for u in result.units:
        if u.block is not None and not u.locked:
            v = check(u.block, c)
            if v:
                problems.append(f"{u.key}: {v[0].code}: {v[0].detail}")
        if u.reason is not None:
            res = search(u.task, c)
            if not isinstance(res, Unschedulable) or res.reason != u.reason.reason:
                problems.append(f"{u.task.id}: claimed unschedulable ({u.reason.reason}) but search says {res}")
    counted: dict[str, int] = {}
    for op in result.ops:
        counted[op["key"]] = counted.get(op["key"], 0) + 1
    from collections import Counter
    for k, n in Counter(result.owned_keys).items():
        ops_n = counted.get(k, 0)
        if ops_n != n:
            problems.append(f"owned block {k}: {n} on the calendar, {ops_n} operation(s)")
    return problems
