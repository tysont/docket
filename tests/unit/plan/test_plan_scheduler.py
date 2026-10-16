"""The scheduler (TD §12.3, §12.4): ordering, batching, keep/move/update/release, move cap and lock,
repair, windows over out-of-office time, window too small, explain."""
from __future__ import annotations

from datetime import timedelta

import pytest

from docket.plan.build import Built, explain
from docket.plan.scheduler import ordering_key, schedule, validate
from docket.plan.types import PlanWindow, task_key
from docket.plan.windows import normalize_when, resolve_window
from tests.plan_helpers import NOW, TODAY, TZ, allday, calendar, d, dt, ev, hm, owned, policy, task

pytestmark = pytest.mark.smoke


def run(tasks, events=(), pol=None, now=NOW):
    p = pol or policy()
    res = schedule(list(tasks), calendar(list(events), horizon=p.horizon_days), p, today=TODAY, now=now, tz=TZ)
    assert validate(res) == []
    return res


def placed(res, tid):
    u = res.unit_for(tid)
    return (u.block.day, u.block.start, u.block.end) if u and u.block else None


def verbs(res):
    return sorted((o["verb"], o["key"]) for o in res.ops)


def test_ordering_imminent_then_overdue_then_due_tier_size():
    p = policy(imminent_days=2)
    ts = [task("late", due=d(-3)), task("soon", due=d(1)), task("later", due=d(9)), task("t2", due=d(9), tier=0, est=30),
          task("none")]
    ts = [t.with_flags(TODAY, p.imminent_days) for t in ts]
    order = [t.id for t in sorted(ts, key=lambda t: ordering_key(t, p))]
    assert order == ["soon", "late", "t2", "later", "none"]


def test_greedy_places_in_order_on_the_earliest_slots():
    res = run([task("b", 60, due=d(5)), task("a", 60, due=d(1))])
    assert placed(res, "a") == (TODAY, hm("09:00"), hm("10:00"))
    assert placed(res, "b") == (TODAY, hm("10:00"), hm("11:00"))
    assert [o["verb"] for o in res.ops] == ["create", "create"]


def test_small_tasks_batch_into_one_daily_block_filling_in_order():
    small = [task(f"s{i}", 15, due=d(3)) for i in range(6)]
    res = run(small + [task("big", 60, due=d(9))], pol=policy(batch_max=15, batch_block_max=60))
    batches = [r for r in res.schedule if r["kind"] == "batch"]
    assert [len(b["task_ids"]) for b in batches] == [4, 2]
    assert batches[0]["date"] == TODAY.isoformat() and batches[1]["date"] == d(1).isoformat()
    assert batches[0]["minutes"] == 60 and batches[1]["minutes"] == 30   # never below one real sitting


def test_keep_update_move_and_release_existing_blocks():
    a, b, c = task("a", 60, due=d(5)), task("b", 60, due=d(5)), task("c", 60, due=d(5))
    k = {t.id: task_key(t, "dk_") for t in (a, b, c)}
    events = [
        owned(k["a"], d(1), "10:00", "11:00", title="Task a"),             # legal and equal: keep
        owned(k["b"], d(1), "11:00", "12:00", title="Old title"),          # legal, title changed: update
        owned(k["c"], d(1), "13:00", "14:00", title="Task c"),             # overlaps a meeting now: move
        ev("m", d(1), "13:00", "14:00", "Design review"),
        owned("dk_t_000000000000", d(1), "15:00", "16:00", title="Done task"),   # no open task: release
    ]
    res = run([a, b, c], events)
    by = {o["key"]: o for o in res.ops}
    assert by[k["a"]]["verb"] == "keep" and by[k["b"]]["verb"] == "update" and by[k["c"]]["verb"] == "move"
    assert by[k["c"]]["from"]["start"].startswith(d(1).isoformat() + "T13:00")
    assert by["dk_t_000000000000"]["verb"] == "release"
    assert len(res.ops) == 4


def test_duplicate_owned_blocks_keep_one_and_release_the_other():
    a = task("a", 60, due=d(5))
    k = task_key(a, "dk_")
    res = run([a], [owned(k, d(1), "10:00", "11:00", title="Task a"), owned(k, d(1), "14:00", "15:00", title="Task a")])
    assert sorted(o["verb"] for o in res.ops) == ["keep", "release"]


def test_move_lock_keeps_blocks_inside_the_lock_even_if_illegal():
    a = task("a", 60, due=d(5))
    k = task_key(a, "dk_")
    events = [owned(k, TODAY, "09:00", "10:00", title="Task a"), ev("m", TODAY, "09:00", "10:00", "Design review")]
    res = run([a], events, pol=policy(move_lock=180))
    assert [(o["verb"], o.get("locked")) for o in res.ops] == [("keep", True)]
    res = run([a], events, pol=policy(move_lock=0))
    assert [o["verb"] for o in res.ops] == ["move"]


def test_elapsed_block_is_locked():
    a = task("a", 60, due=d(5))
    later = dt(TODAY, "12:00")
    res = run([a], [owned(task_key(a, "dk_"), TODAY, "09:00", "10:00", title="Task a")], now=later)
    assert res.ops[0]["verb"] == "keep" and res.ops[0]["locked"]


@pytest.mark.parametrize("cap", [0, 1, 3])
def test_repair_displaces_at_most_move_cap_lower_priority_blocks(cap):
    # The only free hour before the urgent task's due date is held by a kept low-priority block.
    pol = policy(horizon_days=5, move_cap=cap)
    busy = [ev("m0", TODAY, "10:00", "17:00", "Offsite"), ev("m1", d(1), "09:00", "17:00", "Offsite")]
    low = task("low", 60, tier=5)
    urgent = task("urgent", 60, due=TODAY)
    k = task_key(low, "dk_")
    res = run([low, urgent], busy + [owned(k, TODAY, "09:00", "10:00", title="Task low")], pol=pol)
    verb = {o["key"]: o["verb"] for o in res.ops}[k]
    if cap == 0:
        assert placed(res, "low") == (TODAY, 540, 600) and verb == "keep"
        assert placed(res, "urgent")[0] == d(4) and res.unit_for("urgent").late
        assert res.moves_used == 0
    else:
        assert placed(res, "urgent") == (TODAY, 540, 600) and not res.unit_for("urgent").late
        assert placed(res, "low")[0] == d(4) and verb == "move" and res.moves_used == 1


def test_repair_never_displaces_locked_or_higher_priority_blocks():
    pol = policy(horizon_days=5, move_lock=24 * 60)
    busy = [ev("m0", TODAY, "10:00", "17:00", "Offsite"), ev("m1", d(1), "09:00", "17:00", "Offsite")]
    low = task("low", 60, tier=5)
    k = task_key(low, "dk_")
    res = run([low, task("urgent", 60, due=TODAY)], busy + [owned(k, TODAY, "09:00", "10:00", title="Task low")],
              pol=pol)
    assert placed(res, "low") == (TODAY, 540, 600) and res.moves_used == 0


def test_window_over_an_out_of_office_event_places_the_block_on_it():
    trip = ev("f", d(1), "13:00", "16:00", "Flight OOO", attendees=0)
    cal = calendar([trip])
    w = resolve_window(normalize_when("Flight", TODAY), TODAY, instances=cal.instances, lo=TODAY,
                       hi=TODAY + timedelta(days=14), tz=TZ)
    assert w is not None and w.event_id == "f"
    res = run([task("memo", 90, window=w)], [trip])
    assert placed(res, "memo") == (d(1), hm("13:00"), hm("14:30"))
    # without the window the same time is busy
    res = run([task("memo", 90, due=d(1))], [trip, ev("m", TODAY, "09:00", "17:00", "Workshop")])
    assert placed(res, "memo")[1] != hm("13:00") or placed(res, "memo")[0] != d(1)


def test_window_too_small_is_reported_with_free_minutes():
    w = PlanWindow("2026-10-16 13:00-14:00", ((d(1), hm("13:00"), hm("14:00")),), explicit_times=True,
                   explicit_days=True)
    res = run([task("memo", 90, window=w, question_id="q_1")], [ev("m", d(1), "13:00", "13:30", "Design review")])
    assert res.window_errors == [{"task_id": "memo", "question_id": "q_1", "window": w.label, "est": 90,
                                  "free_minutes": 30, "longest_free": 30}]
    assert res.unit_for("memo").block is None


def test_this_week_window_respects_workdays():
    w = resolve_window(normalize_when("this week", TODAY), TODAY)
    busy = [ev(f"m{i}", d(i), "09:00", "17:00", "Workshop") for i in (0, 1)]
    res = run([task("a", 60, window=w)], busy)
    assert res.unit_for("a").block is None and res.window_errors   # Sat and Sun are not workdays


def test_unschedulable_task_with_an_existing_block_is_released_with_its_reason():
    a = task("a", 60, due=d(1))
    k = task_key(a, "dk_")
    busy = [ev(f"m{i}", d(i), "08:00", "18:00", "Workshop") for i in range(14)]
    res = run([a], busy + [owned(k, d(1), "10:00", "11:00", title="Task a")])
    assert res.unschedulable[0]["reason"] == "overlap"
    assert res.ops[0]["verb"] == "release" and res.ops[0]["reason"] == "unschedulable: overlap"


def test_schedule_is_deterministic():
    ts = [task(f"t{i}", 15 * (1 + i % 6), due=d(i % 9), tier=i % 3) for i in range(40)]
    evs = [ev(f"m{i}", d(i % 12), f"{9 + i % 7:02d}:00", f"{10 + i % 7:02d}:00", "Sync") for i in range(30)]
    a = run(ts, evs)
    b = run(list(reversed(ts)), list(reversed(evs)))
    assert a.ops == b.ops and a.schedule == b.schedule and a.unschedulable == b.unschedulable


def _built(res, tasks):
    return Built(inputs=None, cal=res.ctx.cal, tasks=tasks, result=res, mirrors=None, ops=res.ops, notes=[])  # type: ignore[arg-type]


def test_explain_reports_ordering_candidate_days_and_first_violations():
    p = policy(deep_window=(540, 720), lunch=(720, 780))
    ts = [task("deep", 90, due=d(4), title="Draft pricing memo")]
    evs = [ev("m", TODAY, "09:00", "11:00", "Design review")]
    res = run(ts, evs, pol=p)
    out = explain(_built(res, ts), "pricing memo")
    assert out["placed"] == {"date": TODAY.isoformat(), "start": "11:00", "end": "12:30", "late": False,
                             "locked": False, "existing": False} or out["placed"]["date"] == d(1).isoformat()
    assert out["ordering_key"]["due"] == d(4).isoformat()
    assert out["candidate_days"][0] == TODAY.isoformat()
    assert {r["constraint"] for r in out["rejected"]} <= {"overlap", "deep_window", "lunch"}
    assert out["rejected"][0]["constraint"] == "overlap"


def test_explain_of_an_unschedulable_task():
    ts = [task("a", 60, title="Write the summary")]
    res = run(ts, [ev(f"m{i}", d(i), "09:00", "17:00", "Workshop") for i in range(14)])
    out = explain(_built(res, ts), "a")
    assert out["unschedulable"]["reason"] == "overlap"
    assert all(r["constraint"] in ("overlap", "workday") for r in out["rejected"])
