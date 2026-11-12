"""A live plan never places work before the moment it is built (TD §4.6, §12.3, §12.6, §13.1,
FR-PLN-09).

A run pins its clock. A run whose clock was taken from the wall plans from the later of its
pinned instant and the wall clock when the plan is built; a run whose clock was given with
``--now`` plans from that clock only. The wall clock is injected through
``docket.engine.clock.wall_now``: nothing here sleeps.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent

# The run starts on a Thursday evening in Chicago (the instant the persona's recordings were
# made); its plan is built, or approved, the next day.
EVENING = "2026-10-15T18:00:00-05:00"
NEXT_DAY = "2026-10-16"
AFTERNOON = "2026-10-16T13:02:00-05:00"
NOTE = "Planned at 2026-10-16 13:02; nothing is placed before then."


@pytest.fixture
def wall(monkeypatch):
    """The injected wall clock: set ``wall.now`` to move it."""
    from docket.engine import clock

    class Wall:
        now = datetime.fromisoformat(EVENING)
    w = Wall()
    monkeypatch.setattr(clock, "wall_now", lambda: w.now)
    return w


def with_approval(ws):
    y = ws / "docket.yaml"
    text = y.read_text()
    old = "    plan: { plan: [decide], schedule_tasks: true }\n"
    assert old in text
    y.write_text(text.replace(old, old + "    approve: { approve: plan }\n", 1))


def as_tasks(q):
    """Every finding becomes a task the plan places."""
    return next((o for o in q["options"] if o["id"] == "task"), q["options"][0])


def run_dir(ws, run_id):
    return state_root(ws, os.environ) / "runs" / run_id


def build_plan(cli, ws, *start_args):
    code, env = cli("run", "start", "week-plan", *start_args, "--workspace", str(ws))
    assert code == 0, env
    run_id = env["data"]["run"]
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["node"] == "plan", choose=as_tasks)
    assert res.failures == [] and res.final.get("node") == "plan", res.final
    return run_id, res.final


def do_plan(cli, ws, step):
    code, env = cli(*step["command"]["argv"][1:])
    assert code == 0, env
    code, env = cli("plan", "diff", "--workspace", str(ws))
    assert code == 0, env
    return env["data"]


def plan_json(ws, run_id, pd):
    return json.loads((run_dir(ws, run_id) / "plan" / pd / "plan.json").read_text())


def next_day_starts(plan):
    """The starts of the blocks the plan creates or moves to on the next day."""
    return sorted(o["start"] for o in plan["operations"]
                  if o.get("verb") in ("create", "move") and o.get("start", "").startswith(NEXT_DAY))


def test_a_plan_built_hours_into_a_live_run_places_nothing_before_it_was_built(persona, cli, wall):
    early = persona("reference-like", "early")
    run_id, step = build_plan(cli, early)
    header = json.loads((run_dir(early, run_id) / "run.json").read_text())
    assert header["clock"]["given"] is False and header["clock"]["instant"] == EVENING
    plan = plan_json(early, run_id, do_plan(cli, early, step)["plan_digest"])
    assert any(s < AFTERNOON for s in next_day_starts(plan)), next_day_starts(plan)  # the morning had room
    assert plan["planned_at"] == EVENING and not any("Planned at" in n for n in plan["notes"])

    late = persona("reference-like", "late")
    run_id, step = build_plan(cli, late)
    wall.now = datetime.fromisoformat(AFTERNOON)            # the plan is built the next afternoon
    diff = do_plan(cli, late, step)
    plan = plan_json(late, run_id, diff["plan_digest"])
    assert plan["planned_at"] == AFTERNOON
    assert next_day_starts(plan) and all(s >= AFTERNOON for s in next_day_starts(plan)), next_day_starts(plan)
    assert NOTE in plan["notes"] and NOTE in diff["notes"]
    header = json.loads((run_dir(late, run_id) / "run.json").read_text())
    assert header["clock"]["instant"] == EVENING          # the run's own clock is unchanged
    code, env = cli("plan", "explain", "headcount", "--workspace", str(late))
    assert code == 0, env
    placed = env["data"]["explain"]["placed"]
    assert (placed["date"], placed["start"]) >= (NEXT_DAY, "13:02"), placed
    wall.now = datetime.fromisoformat(f"{NEXT_DAY}T16:45:00-05:00")   # explain reproduces the plan as built
    code, env = cli("plan", "explain", "headcount", "--workspace", str(late))
    assert code == 0 and env["data"]["explain"]["placed"] == placed, env


def test_a_run_whose_clock_was_given_plans_from_that_clock_only(persona, cli, wall):
    ws = persona("reference-like")
    run_id, step = build_plan(cli, ws, "--now", EVENING)
    header = json.loads((run_dir(ws, run_id) / "run.json").read_text())
    assert header["clock"]["given"] is True
    wall.now = datetime.fromisoformat(AFTERNOON)
    plan = plan_json(ws, run_id, do_plan(cli, ws, step)["plan_digest"])
    assert plan["planned_at"] == EVENING and any(s < AFTERNOON for s in next_day_starts(plan))
    assert not any("Planned at" in n for n in plan["notes"])


def test_approval_after_a_block_s_start_passed_is_fresh_and_names_the_block(persona, cli, wall):
    ws = persona("reference-like")
    with_approval(ws)
    run_id, step = build_plan(cli, ws)
    plan = plan_json(ws, run_id, do_plan(cli, ws, step)["plan_digest"])
    first = next_day_starts(plan)[0]
    code, env = cli("next", "--workspace", str(ws))
    (ask,) = [s for s in env["data"]["steps"] if s["kind"] == "ask"]
    assert "passed" not in ask["questions"][0]["details"]

    wall.now = datetime.fromisoformat(first).replace(minute=datetime.fromisoformat(first).minute + 1)
    code, env = cli("next", "--workspace", str(ws))
    (ask,) = [s for s in env["data"]["steps"] if s["kind"] == "ask"]
    q = ask["questions"][0]
    passed = q["details"]["passed"]                        # compact form: a driver tells the operator
    assert [p["start"] for p in passed] == [first] and passed[0]["title"] and passed[0]["verb"] == "create"
    code, full = cli("next", "--full", "--workspace", str(ws))
    (fask,) = [s for s in full["data"]["steps"] if s["kind"] == "ask"]
    assert fask["questions"][0]["details"]["passed"] == passed
    code, env = cli("ask", "answer", q["id"], "yes", "--digest", q["details"]["digest"], "--workspace", str(ws))
    assert code == 0, env                                  # time passing alone does not make the plan stale
    code, env = cli("next", "--workspace", str(ws))
    assert {n["node"]: n["state"] for n in env["data"]["nodes"]}["approve"] == "current"
