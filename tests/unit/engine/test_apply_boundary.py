"""The apply boundary (TD §5.4): once a write has begun, the nodes upstream of it are read from
their pins. A write that changes what an upstream node reads must not stale that node and run
the write again; the run moves on to the seal. All data is invented."""
from __future__ import annotations

import os

from docket.config import load_workspace
from docket.engine.context import RunContext
from docket.engine.nodes import join
from docket.store.paths import state_root
from docket.store.runs import Store, load_run
from tests.agent_loop import run_agent

WEEK_PLAN_APPLIED = ("    plan: { plan: [decide], schedule_tasks: true }\n"
                     "    approve: { approve: plan }\n"
                     "    apply: { apply: approve }\n")


def test_a_write_that_changes_what_its_upstream_reads_runs_once(persona, cli, pinned_now):
    ws = persona("reference-like")
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    assert "    plan: { plan: [decide], schedule_tasks: true }\n" in text
    text = text.replace("    plan: { plan: [decide], schedule_tasks: true }\n", WEEK_PLAN_APPLIED)
    # the basic options: the standard set offers calendar and message actions no destination here accepts
    cfg.write_text(text.replace("decide: { ask: standard,", "decide: { ask: basic,"))
    dests = ws / "destinations.yaml"
    dests.write_text(dests.read_text() + "  work-calendar:\n    adapter: calendar_file\n    path: Calendar.ics\n")
    code, env = cli("run", "start", "week-plan", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    run_id = env["data"]["run"]
    before = (ws / "Backlog.md").read_text()

    res = run_agent(ws, stop_at=lambda s: s["node"] == "apply")
    assert res.failures == [] and res.final["kind"] == "run", res.final
    code, env = cli("do", "apply", "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] is True, env
    applied = env["data"]["result"]["receipts"]
    assert {r["outcome"] for r in applied} <= {"applied", "already_present"}, applied
    assert any(r["destination"] == "backlog" and r["outcome"] == "applied" for r in applied)
    # the write changed the task store the scheduling plan node reads
    assert (ws / "Backlog.md").read_text() != before
    # ... so a fresh evaluation of plan differs from its pin, yet the run reads plan as pinned
    rc = RunContext(load_workspace(ws), load_run(Store(state_root(ws, os.environ)), run_id), os.environ)
    pinned = rc.eval("plan")
    assert pinned.extra.get("pinned") is True
    fresh = rc.impls["plan"].evaluate(rc, join([rc.eval(n) for n in rc.inputs("plan")]))
    assert fresh.digest != pinned.digest

    assert [(s["kind"], s["node"]) for s in env["data"]["steps"]] == [("run", "seal")]
    code, env = cli("next", "--workspace", str(ws))
    nodes = {n["node"]: n for n in env["data"]["nodes"]}
    assert nodes["plan"]["state"] == "current" and nodes["apply"]["state"] == "current"
    assert [(s["kind"], s["node"]) for s in env["data"]["steps"]] == [("run", "seal")]

    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"
    assert not any(s["node"] == "apply" for s in done.steps)  # apply never ran a second time
    assert done.final["summary"]["operations"] == len(applied)
