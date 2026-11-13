"""Standing blocks (TD §12.9, FR-PLN-11): a block a workspace wants every working day, placed
before ordinary tasks in the earliest legal slot of its day, under a key of the entry and
the day. All data is invented."""
from __future__ import annotations

import pytest

from docket.config.models import Planning
from docket.plan.scheduler import Standing, schedule, standing_day, validate
from docket.plan.types import Task, task_key
from tests.plan_helpers import NOW, TODAY, TZ, calendar, d, dt, ev, owned, policy, task

pytestmark = pytest.mark.smoke

PLAN = Standing(id="plan-day", title="Plan the day", est=30, on="next_workday", notes="Agenda first.")


def run(tasks=(), events=(), *, standing=(PLAN,), pol=None, now=NOW, today=TODAY):
    p = pol or policy()
    res = schedule(list(tasks), calendar(list(events), horizon=p.horizon_days), p, today=today, now=now, tz=TZ,
                   standing=list(standing))
    assert validate(res) == []
    return res


def key(day, sid="plan-day"):
    return task_key(Task(id=f"{sid}@{day.isoformat()}", title="", est=30, kind="standing"), "dk_")


def test_the_next_workday_skips_the_weekend():
    p = policy()
    assert standing_day("next_workday", d(0), p.workdays) == d(1)       # Thursday -> Friday
    assert standing_day("next_workday", d(1), p.workdays) == d(4)       # Friday -> Monday
    assert standing_day("today", d(0), p.workdays) == d(0)


def test_a_standing_block_takes_the_earliest_legal_slot_of_its_day_before_tasks_are_placed():
    busy = [ev("m", d(1), "09:00", "10:00", "Team sync")]
    pol = policy(lunch=(720, 780), meeting_buffer=15, prefer_window=(840, 1020), daily_target=60)
    res = run([task("a", 60, due=d(1))], busy, pol=pol, now=dt(TODAY, "17:30"))
    (op,) = [o for o in res.ops if o["kind"] == "standing"]
    assert (op["verb"], op["key"], op["start"]) == ("create", key(d(1)), dt(d(1), "10:15").isoformat())
    assert (op["title"], op["notes"], op["task_ids"], op["standing"]) == ("Plan the day", "Agenda first.", [], "plan-day")
    a = res.unit_for("a").block
    assert a.day == d(1) and not (a.start < 645 and 615 < a.end)        # the task plans around it


def test_tomorrows_run_keeps_todays_block_and_plans_the_next_one():
    first = run()
    (made,) = first.ops
    assert made["key"] == key(d(1))
    today_block = owned(key(d(1)), d(1), "09:00", "09:30", title="Plan the day", notes="Agenda first.")
    second = schedule([], calendar([today_block]), policy(), today=d(1), now=dt(d(1), "08:00"), tz=TZ,
                      standing=[PLAN])
    assert validate(second) == []
    assert sorted((o["verb"], o["key"]) for o in second.ops) == sorted([("keep", key(d(1))), ("create", key(d(4)))])


def test_a_standing_block_is_never_batched_split_or_late():
    short = Standing(id="check-in", title="Check in", est=10, on="next_workday")
    long = Standing(id="review", title="Review", est=180, on="next_workday")
    res = run(standing=[short, long], pol=policy(batch_max=15, split_over=60, max_block=240))
    kinds = sorted((o["standing"], o["kind"], o["end"][11:16]) for o in res.ops)
    assert kinds == [("check-in", "standing", "09:30"), ("review", "standing", "12:30")]
    full = [ev("m", d(1), "09:00", "17:00", "Offsite")]
    blocked = run(events=full)
    assert blocked.ops == [] and blocked.unschedulable == []
    assert any("plan-day" in n and d(1).isoformat() in n for n in blocked.notes)


def test_a_standing_block_whose_entry_was_removed_is_released():
    res = run(events=[owned(key(d(1)), d(1), "09:00", "09:30", title="Plan the day")], standing=())
    (op,) = res.ops
    assert op["verb"] == "release" and "standing" in op["reason"]


def test_standing_entries_are_checked():
    ok = Planning.model_validate({"standing": [{"id": "plan-day", "title": "Plan the day", "est": "30m"}]})
    assert ok.standing[0].on == "next_workday"
    for bad in ([{"id": "a", "title": "A", "est": "30m"}, {"id": "a", "title": "B", "est": "15m"}],
                [{"id": "a", "title": "A"}],
                [{"id": "a", "title": "A", "est": "soon"}],
                [{"id": "a", "title": "A", "est": "30m", "on": "tomorrow"}],
                [{"id": "Plan Day", "title": "A", "est": "30m"}]):
        with pytest.raises(ValueError):
            Planning.model_validate({"standing": bad})
