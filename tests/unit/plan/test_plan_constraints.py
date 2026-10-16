"""The constraint engine (TD §12.2): one table row per constraint, free intervals, search."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from docket.plan.constraints import check, free_intervals, search
from docket.plan.types import Block, Placement, PlanWindow, Unschedulable
from tests.plan_helpers import NOW, TODAY, TZ, allday, ctx, d, ev, hm, policy, task

pytestmark = pytest.mark.smoke


def blk(day=None, start="10:00", end="11:00", kind="shallow", key="k1", **kw):
    return Block(key=key, kind=kind, day=day or d(1), start=hm(start), end=hm(end), **kw)


MEETING = ev("m", d(1), "13:00", "14:00", "Design review")


@pytest.mark.parametrize("block,pol,code", [
    (blk(day=d(20)), {}, "horizon"),
    (blk(day=d(2)), {}, "workday"),                                 # Saturday
    (blk(day=TODAY, start="07:30", end="08:00"), {"day_window": (0, 1440)}, "past"),
    (blk(earliest=d(3)), {}, "earliest"),
    (blk(window=PlanWindow("x", ((d(4), 540, 600),))), {}, "window"),
    (blk(start="08:00", end="09:00"), {}, "day_window"),
    (blk(start="10:05", end="11:05"), {}, "granularity"),
    (blk(start="10:00", end="10:15"), {}, "min_block"),
    (blk(start="09:00", end="12:00"), {}, "max_block"),
    (blk(start="14:00", end="15:00", kind="deep"), {"deep_window": (540, 720)}, "deep_window"),
    (blk(start="11:30", end="12:30"), {"lunch": (720, 780)}, "lunch"),
    (blk(start="13:30", end="14:30"), {}, "overlap"),
    (blk(start="14:00", end="15:00"), {"meeting_buffer": 10}, "buffer"),
])
def test_each_constraint(block, pol, code):
    c = ctx([MEETING], pol=policy(**pol))
    v = check(block, c)
    assert [x.code for x in v] == [code], v


def test_violations_are_listed_in_constraint_order():
    assert [v.code for v in check(blk(day=d(-1)), ctx())] == ["horizon", "past"]


def test_a_legal_block_has_no_violations():
    assert check(blk(), ctx([MEETING])) == []


def test_overlap_with_placed_blocks_daily_cap_and_one_batch_per_day():
    c = ctx(pol=policy(daily_cap=90))
    c.add(blk(key="a", start="09:00", end="10:00", kind="batch"))
    assert [v.code for v in check(blk(key="b", start="09:30", end="10:30"), c)] == ["overlap", "daily_cap"]
    assert [v.code for v in check(blk(key="b", start="11:00", end="12:00"), c)] == ["daily_cap"]
    assert [v.code for v in check(blk(key="b", start="11:00", end="11:30", kind="batch"), c)] == ["batch_per_day"]
    assert check(blk(key="a", start="09:00", end="10:00", kind="batch"), c) == []   # itself, checked in place


def test_soft_holds_and_ooo_are_busy_unless_the_block_has_a_window():
    hold = ev("h", d(1), "10:00", "12:00", "Hold: maybe", attendees=0)
    c = ctx([hold, allday("o", d(4), title="Trip OOO")])
    assert [v.code for v in check(blk(), c)] == ["overlap"]
    w = PlanWindow("thu", ((d(1), 0, 1440),), explicit_days=True)
    assert check(blk(window=w), c) == []
    w2 = PlanWindow("event:Trip", ((d(4), 0, 1440),), explicit_times=True, explicit_days=True)
    assert check(blk(day=d(4), start="18:00", end="19:00", window=w2), c) == []


def test_free_intervals_subtracts_lunch_buffer_busy_overlay_and_past():
    p = policy(lunch=(720, 780), meeting_buffer=15)
    c = ctx([MEETING], pol=p)
    segs = c.cal.segments(d(1))
    free = free_intervals(d(1), p, segs, window=None, overlay=[blk(start="09:00", end="10:00")])
    assert free == [(600, 720), (855, 1020)]
    assert free_intervals(d(2), p, [], window=None, overlay=[]) == []          # Saturday
    now = datetime.combine(TODAY, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10, minutes=7)
    c2 = ctx(pol=p, now=now)
    assert c2.free(TODAY)[0] == (607, 720)


def test_incremental_free_matches_recomputation():
    c = ctx([MEETING])
    before = list(c.free(d(1)))
    b = blk(start="10:00", end="11:00")
    c.add(b)
    assert c.free(d(1)) == free_intervals(d(1), c.policy, c.cal.segments(d(1)), window=None, overlay=[b])
    c.remove(b)
    assert c.free(d(1)) == before


def test_search_takes_the_earliest_legal_slot_and_flags_late():
    c = ctx([ev("m1", TODAY, "08:00", "17:00", "All-day workshop")])
    r = search(task("a", 60, due=TODAY), c)
    assert isinstance(r, Placement) and r.late and r.block.day == d(1) and r.block.start == 540
    r = search(task("b", 60, due=d(5)), c)
    assert isinstance(r, Placement) and not r.late and r.block.day == d(1)


def test_search_respects_deep_window_and_earliest():
    c = ctx(pol=policy(deep_window=(600, 720)))
    r = search(task("a", 90, earliest=d(4)), c)
    assert isinstance(r, Placement) and r.block.day == d(4) and r.block.start == 600 and r.block.kind == "deep"


def test_unschedulable_names_the_tightest_constraint():
    full = [ev(f"m{i}", d(i), "09:00", "17:00", "Workshop") for i in range(14)]
    r = search(task("a", 60), ctx(full))
    assert isinstance(r, Unschedulable) and r.reason == "overlap"
    r = search(task("b", 60), ctx(pol=policy(daily_cap=30)))
    assert isinstance(r, Unschedulable) and r.reason == "daily_cap"
    r = search(task("c", 300), ctx())
    assert isinstance(r, Unschedulable) and r.reason == "max_block"
    r = search(task("d", 60, earliest=d(30)), ctx())
    assert isinstance(r, Unschedulable) and r.reason == "horizon"
