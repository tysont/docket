"""Plan build, 200 tasks over 42 days: under 5 s (TP §10, NFR-06, TD §18)."""
from __future__ import annotations

import time

import pytest

from docket.plan.scheduler import schedule, validate
from docket.plan.types import task_key
from tests.plan_helpers import NOW, TODAY, TZ, calendar, d, ev, owned, policy, task

pytestmark = pytest.mark.slow


def test_200_tasks_over_42_days_plan_in_under_5_seconds():
    pol = policy(horizon_days=42, deep_window=(540, 720), lunch=(720, 780), meeting_buffer=5, daily_cap=300,
                 batch_max=15, batch_block_max=60)
    tasks = [task(f"t{i:03d}", [15, 30, 45, 60, 90, 120][i % 6], due=d(i % 40) if i % 4 else None, tier=i % 3)
             for i in range(200)]
    events = []
    for day in range(42):
        for h, title in ((10, "Standup"), (14, "Review"), (16, "Sync")):
            if (day + h) % 3:
                events.append(ev(f"m{day}-{h}", d(day), f"{h:02d}:00", f"{h:02d}:30", title))
    for i, t in enumerate(tasks[:40]):
        events.append(owned(task_key(t, "dk_"), d(i % 10), "15:00", "15:30", title=t.title))
    cal = calendar(events, horizon=42)
    t0 = time.perf_counter()
    res = schedule(tasks, cal, pol, today=TODAY, now=NOW, tz=TZ)
    elapsed = time.perf_counter() - t0
    print(f"plan build 200 tasks / 42 days: {elapsed:.2f} s, {len(res.schedule)} blocks, "
          f"{len(res.unschedulable)} unschedulable")
    assert validate(res) == []
    assert len(res.schedule) > 100
    assert elapsed < 5.0
