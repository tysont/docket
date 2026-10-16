"""Property tests of the one constraint engine (TP §5.4, FR-PLN-05, FR-PLN-06).

Hypothesis generates calendars (meetings, holds, out-of-office, recurring series, multi-day spans,
tombstones, owned blocks), policies within valid ranges and task sets, then asserts:

- every block in a built plan has no violations under ``check``;
- every task the plan calls unschedulable has ``search`` returning unschedulable with the same reason;
- no owned block in the input lacks an operation in the output, and none has two;
- plan build is deterministic, including under input reordering.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from docket.plan.constraints import check, search
from docket.plan.scheduler import schedule, validate
from docket.plan.types import Event, PlanWindow, Unschedulable, task_key
from tests.plan_helpers import TODAY, TZ, calendar, d, policy, task

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def policies(draw):
    g = draw(st.sampled_from([5, 10, 15, 30]))
    min_block = g * draw(st.integers(1, 4))
    max_block = min_block + g * draw(st.integers(0, 8))
    start = draw(st.sampled_from([420, 480, 540]))
    end = start + draw(st.sampled_from([360, 480, 600]))
    lunch = draw(st.sampled_from([None, (720, 780), (690, 750)]))
    deep = draw(st.sampled_from([None, (start, start + 180)]))
    return policy(
        granularity=g, min_block=min_block, max_block=max_block, day_window=(start, end), lunch=lunch,
        deep_window=deep, meeting_buffer=draw(st.sampled_from([0, 5, 10, 15])),
        daily_cap=draw(st.sampled_from([None, 120, 240])), horizon_days=draw(st.integers(3, 21)),
        move_cap=draw(st.integers(0, 3)), move_lock=draw(st.sampled_from([0, 60, 600])),
        batch_max=draw(st.sampled_from([0, 15, 30])), batch_block_max=draw(st.sampled_from([30, 60, 90])),
        workdays=frozenset(draw(st.sets(st.integers(0, 6), min_size=1, max_size=7))),
    )


def _dt(day, minutes):
    return datetime.combine(day, time(0), tzinfo=TZ) + timedelta(minutes=minutes)


@st.composite
def events(draw, tasks, pol):
    task_keys = [task_key(t, "dk_") for t in tasks]
    out = []
    n = draw(st.integers(0, 25))
    for i in range(n):
        kind = draw(st.sampled_from(["meeting", "hold", "ooo", "ooo_allday", "series", "span", "tombstone", "owned",
                                     "orphan", "focus", "info"]))
        day = d(draw(st.integers(-3, 20)))
        s = draw(st.integers(0, 95)) * 15
        length = draw(st.integers(1, 16)) * 15
        title = {"meeting": "Sync", "hold": "Hold", "ooo": "Trip OOO", "focus": "Focus"}.get(kind, "Event")
        props = {"attendees": 3} if kind in ("meeting", "series", "span") else {}
        if kind == "ooo_allday":
            out.append(Event(f"e{i}", "work", "Away OOO", day, day + timedelta(days=draw(st.integers(1, 3))),
                             all_day=True))
        elif kind == "series":
            first = TODAY - timedelta(weeks=draw(st.integers(0, 8)))
            out.append(Event(f"e{i}", "work", "Weekly", _dt(first, s), _dt(first, s + length), properties=props,
                             recurrence={"freq": draw(st.sampled_from(["weekly", "daily"])),
                                         "byday": draw(st.lists(st.sampled_from(["mon", "tue", "thu"]), max_size=2))}))
            if draw(st.booleans()):
                occ = d(draw(st.integers(0, 14)))
                out.append(Event(f"e{i}x", "work", "", _dt(occ, s), _dt(occ, s + length), status="cancelled",
                                 series_id=f"e{i}", original_start=_dt(occ, s)))
        elif kind == "span":
            out.append(Event(f"e{i}", "work", "Offsite", _dt(day, s), _dt(day + timedelta(days=draw(st.integers(1, 56))),
                                                                           s), properties=props))
        elif kind == "tombstone":
            out.append(Event(f"e{i}", "work", "Sync", _dt(day, s), _dt(day, s + length), status="cancelled",
                             properties=props))
        elif kind in ("owned", "orphan"):
            g = pol.granularity
            s = pol.day_window[0] + g * draw(st.integers(0, (pol.day_window[1] - pol.day_window[0]) // g - 1))
            length = g * draw(st.integers(1, 8))
            if kind == "owned" and tasks:
                j = draw(st.integers(0, len(tasks) - 1))
                key = task_keys[j]
                if draw(st.booleans()):
                    length = pol.length_for(tasks[j])
            else:
                key = f"dk_t_orphan{i:06d}"
            day = d(draw(st.integers(0, 6)))
            out.append(Event(f"e{i}", "work", "Block", _dt(day, s), _dt(day, min(s + length, 1440)), owned_key=key))
        elif kind == "info":
            out.append(Event(f"e{i}", "work", "FYI", _dt(day, s), _dt(day, s + length), transparency="transparent"))
        else:
            out.append(Event(f"e{i}", "work", title, _dt(day, s), _dt(day, min(s + length, 1440)), properties=props))
    return out


@st.composite
def scenarios(draw):
    pol = draw(policies())
    n = draw(st.integers(0, 25))
    tasks = []
    for i in range(n):
        due = draw(st.one_of(st.none(), st.integers(-3, 25).map(d)))
        earliest = draw(st.one_of(st.none(), st.integers(0, 10).map(d)))
        window = None
        if draw(st.integers(0, 6)) == 0:
            wd = d(draw(st.integers(0, 10)))
            if draw(st.booleans()):
                a = draw(st.integers(28, 64)) * 15
                window = PlanWindow(f"w{i}", ((wd, a, a + draw(st.integers(2, 16)) * 15),), explicit_times=True,
                                    explicit_days=True)
            else:
                window = PlanWindow(f"w{i}", ((wd, 0, 1440),), explicit_days=True)
        est = draw(st.one_of(st.integers(5, 60), st.integers(5, 60), st.integers(5, 200)))
        tasks.append(task(f"t{i:02d}", est, due=due, earliest=earliest,
                          tier=draw(st.integers(0, 3)), window=window))
    evs = draw(events(tasks, pol))
    now = datetime.combine(TODAY, time(0), tzinfo=TZ) + timedelta(minutes=draw(st.integers(0, 1439)))
    return pol, tasks, evs, now


def build(pol, tasks, evs, now):
    return schedule(tasks, calendar(evs, horizon=pol.horizon_days), pol, today=TODAY, now=now, tz=TZ)


@SETTINGS
@given(scenarios())
def test_every_built_block_passes_check(sc):
    res = build(*sc)
    full = res.ctx.copy(overlay=[u.block for u in res.units if u.block is not None])
    for u in res.units:
        if u.block is not None and not u.locked:
            assert check(u.block, full) == [], (u.task, u.block)


@SETTINGS
@given(scenarios())
def test_every_unschedulable_claim_is_confirmed_by_search(sc):
    res = build(*sc)
    full = res.ctx.copy(overlay=[u.block for u in res.units if u.block is not None])
    for row in res.unschedulable:
        u = res.unit_for(row["task_id"])
        again = search(u.task, full)
        assert isinstance(again, Unschedulable) and again.reason == row["reason"], (row, again)


@SETTINGS
@given(scenarios())
def test_every_owned_input_block_gets_exactly_one_operation(sc):
    pol, tasks, evs, now = sc
    res = build(*sc)
    cal = calendar(evs, horizon=pol.horizon_days)
    owned_in = Counter(ev.owned_key for ev, _ in cal.owned)
    ops = Counter(o["key"] for o in res.ops if o["verb"] != "create")
    assert owned_in == ops
    assert validate(res) == []


@SETTINGS
@given(scenarios())
def test_plan_build_is_deterministic(sc):
    pol, tasks, evs, now = sc
    a = build(pol, tasks, evs, now)
    b = build(pol, list(reversed(tasks)), list(reversed(evs)), now)
    assert a.ops == b.ops and a.schedule == b.schedule and a.unschedulable == b.unschedulable
