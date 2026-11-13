"""A carried task moves its old block (TD §12.3, FR-PLN-10).

A block's key derives from its task, so a task placed again after its block elapsed has
the key the elapsed block still carries on the calendar. The scheduler sees owned blocks of
the days before the run (``earlier``) and the elapsed blocks whose work did not happen
(``redo``), and moves that block to the new slot instead of creating a second one under the
same key. All data is invented."""
from __future__ import annotations

import pytest

from docket.plan.scheduler import make_batches, schedule, split_long, validate
from docket.plan.types import task_key
from tests.plan_helpers import NOW, TODAY, TZ, calendar, ctx, d, dt, owned, policy, task

pytestmark = pytest.mark.smoke


def run(tasks, events=(), *, earlier=(), redo=(), pol=None, now=NOW):
    p = pol or policy()
    res = schedule(list(tasks), calendar(list(events), horizon=p.horizon_days), p, today=TODAY, now=now, tz=TZ,
                   earlier=list(earlier), redo=frozenset(redo))
    assert validate(res) == []
    return res


def ops_for(res, key):
    return [o for o in res.ops if o["key"] == key]


def test_a_task_whose_block_elapsed_on_an_earlier_day_moves_that_block():
    a = task("a", 60, due=d(5))
    k = task_key(a, "dk_")
    res = run([a], earlier=[owned(k, d(-1), "10:00", "11:00", title="Task a")])
    (op,) = ops_for(res, k)
    assert op["verb"] == "move"
    assert op["from"]["start"] == dt(d(-1), "10:00").isoformat()
    assert op["start"] == dt(TODAY, "09:00").isoformat()
    assert res.schedule[0]["status"] == "moved"


def test_a_moved_block_whose_words_changed_is_updated_after_the_move():
    a = task("a", 60, due=d(5), title="Task a, again")
    k = task_key(a, "dk_")
    res = run([a], earlier=[owned(k, d(-1), "10:00", "11:00", title="Task a")])
    assert [(o["verb"], o.get("follows")) for o in ops_for(res, k)] == [("move", None), ("update", "move")]
    upd = ops_for(res, k)[1]
    assert upd["title"] == "Task a, again" and upd["start"] == ops_for(res, k)[0]["start"]


def test_a_block_elapsed_today_moves_when_its_work_did_not_happen_and_stays_otherwise():
    a = task("a", 60, due=d(5))
    k = task_key(a, "dk_")
    noon = dt(TODAY, "12:00")
    block = owned(k, TODAY, "09:00", "10:00", title="Task a")
    kept = run([a], [block], now=noon)
    assert [(o["verb"], o.get("locked")) for o in kept.ops] == [("keep", True)]
    moved = run([a], [block], redo={k}, now=noon)
    (op,) = moved.ops
    assert op["verb"] == "move" and op["start"] == dt(TODAY, "12:00").isoformat()
    assert op["from"]["start"] == dt(TODAY, "09:00").isoformat()


def test_an_earlier_block_with_no_open_task_is_left_as_it_is():
    a = task("a", 60, due=d(5))
    res = run([a], earlier=[owned("dk_t_000000000000", d(-1), "10:00", "11:00", title="Done task")])
    assert ops_for(res, "dk_t_000000000000") == []
    assert [o["verb"] for o in res.ops] == ["create"]


def test_a_block_on_the_calendar_today_wins_over_an_earlier_one_with_the_same_key():
    a = task("a", 60, due=d(5))
    k = task_key(a, "dk_")
    res = run([a], [owned(k, TODAY, "14:00", "15:00", title="Task a")],
              earlier=[owned(k, d(-1), "10:00", "11:00", title="Task a")])
    assert [o["verb"] for o in ops_for(res, k)] == ["keep"]


def test_a_carried_batch_with_the_same_members_moves_and_a_changed_one_leaves_the_old_block():
    p = policy(batch_max=15, batch_block_max=60)
    small = [task(f"s{i}", 15, due=d(3)) for i in range(3)]
    (batch,), _ = make_batches(small, ctx(pol=p), None)
    k = task_key(batch, "dk_")
    old = owned(k, d(-1), "09:00", "09:45", title="3 small tasks")
    same = run(small, earlier=[old], pol=p)
    assert [o["verb"] for o in ops_for(same, k)] == ["move"]
    changed = run(small[:2], earlier=[old], pol=p)                  # one member closed: another batch
    assert ops_for(changed, k) == []
    assert [o["verb"] for o in changed.ops] == ["create"] and changed.ops[0]["key"] != k


def test_the_parts_of_a_split_task_move_their_own_blocks():
    p = policy(split_over=90)
    big = task("big", 180, due=d(9))
    parts = split_long([big], p)
    keys = [task_key(x, "dk_") for x in parts]
    res = run([big], earlier=[owned(keys[0], d(-2), "09:00", "10:30", title="Task big (1/2)"),
                              owned(keys[1], d(-1), "09:00", "10:30", title="Task big (2/2)")], pol=p)
    assert [[o["verb"] for o in ops_for(res, k)] for k in keys] == [["move"], ["move"]]
    days = [ops_for(res, k)[0]["start"][:10] for k in keys]
    assert days[0] < days[1]                                        # a part still follows the one before it
    assert all(o["task_ids"] == ["big"] for o in res.ops)


def test_a_later_part_moves_after_an_earlier_part_placed_again():
    """Part 1 elapsed and is placed again; part 2 still waits ahead of it. Parts keep their
    order, so part 2 is placed again after part 1 rather than kept before it."""
    p = policy(split_over=90)
    big = task("big", 180, due=d(9))
    one, two = (task_key(x, "dk_") for x in split_long([big], p))
    res = run([big], [owned(two, TODAY, "14:00", "15:30", title="Task big (2/2)")],
              earlier=[owned(one, d(-1), "09:00", "10:30", title="Task big (1/2)")], pol=p)
    first, second = (ops_for(res, k)[0] for k in (one, two))
    assert first["verb"] == "move" and second["verb"] == "move"
    assert first["start"][:10] < second["start"][:10]


def test_an_unplaced_task_leaves_its_earlier_block_alone():
    a = task("a", 600, due=d(5))                                    # over the maximum block: unschedulable
    k = task_key(a, "dk_")
    res = run([a], earlier=[owned(k, d(-1), "10:00", "11:00", title="Task a")])
    assert ops_for(res, k) == [] and res.unschedulable
