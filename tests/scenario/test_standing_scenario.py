"""Standing blocks through the CLI (TD §12.9, FR-PLN-11).

A workspace wants a planning session every working day: thirty minutes in the earliest free
slot of the next working day. Day 1 (a Thursday) places Friday's block first and the tasks
around it. Day 2 (Friday, after the block) keeps Friday's block, asks no reconcile question
about it, and places Monday's. The item ledger never names a standing block. All data is
invented.
"""
from __future__ import annotations

import os
import sqlite3
from collections import Counter

from docket.store.paths import state_root
from tests.agent_loop import fill_template, run_agent
from tests.scenario.test_carry_scenario import DAY1, events, plan_ops, setup

FRIDAY_LATER = "2026-10-16T10:00:00-05:00"
STANDING = "  standing:\n    - { id: plan-day, title: Plan the day, est: 30m, on: next_workday, notes: Agenda first. }\n"


def test_a_standing_block_is_placed_first_kept_the_next_day_and_never_reconciled(persona, cli, tmp_path):
    ws = persona("reference-like")
    ics = setup(ws, "ics", tmp_path)
    plan = ws / "planning.yaml"
    plan.write_text(plan.read_text().replace("  policy:\n", STANDING + "  policy:\n", 1))
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env.get("error")

    code, env = cli("run", "start", "triage", "--now", DAY1, "--workspace", str(ws))
    run1 = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    ops1 = plan_ops(ws, run1)
    (friday,) = [o for o in ops1 if o.get("standing")]
    assert (friday["verb"], friday["title"], friday["notes"], friday["task_ids"]) == \
        ("create", "Plan the day", "Agenda first.", [])
    assert friday["start"] == "2026-10-16T09:00:00-05:00" and friday["end"] == "2026-10-16T09:30:00-05:00"
    for o in ops1:   # every task block on Friday plans around it
        if o is not friday and o.get("start", "").startswith("2026-10-16"):
            assert not (o["start"] < friday["end"] and friday["start"] < o["end"]), o

    code, env = cli("run", "start", "triage", "--now", FRIDAY_LATER, "--workspace", str(ws))
    run2 = env["data"]["run"]
    step = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True).final
    assert step["kind"] == "ask", step
    assert not any(q["details"].get("block_key") == friday["key"] for q in step["questions"])
    for q in step["questions"]:
        opt = "done" if q["kind"] == "reconcile" else q["options"][0]["id"]
        code, env = cli(*fill_template(step["answer"]["argv"], q["id"], opt)[1:])
        assert code == 0, env
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done", done.final
    standing2 = sorted((o["verb"], o["start"][:10]) for o in plan_ops(ws, run2) if o.get("standing"))
    assert standing2 == [("create", "2026-10-19"), ("keep", "2026-10-16")]

    per_key = Counter(e["key"] for e in events(ics))
    keys = {o["key"] for r in (run1, run2) for o in plan_ops(ws, r) if o.get("standing")}
    assert len(keys) == 2 and all(per_key[k] == 1 for k in keys)
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    try:
        text = "\n".join(str(r) for t in ("ledgers", "item_ledger", "dispositions")
                         for r in db.execute(f"SELECT * FROM {t}"))
    finally:
        db.close()
    assert not any(k in text for k in keys)


def test_removing_the_entry_releases_its_blocks(persona, cli, tmp_path):
    ws = persona("reference-like")
    ics = setup(ws, "ics", tmp_path)
    plan = ws / "planning.yaml"
    plan.write_text(plan.read_text().replace("  policy:\n", STANDING + "  policy:\n", 1))
    code, env = cli("run", "start", "triage", "--now", DAY1, "--workspace", str(ws))
    assert run_agent(ws).final["kind"] == "done"
    plan.write_text(plan.read_text().replace(STANDING, ""))
    code, env = cli("run", "start", "triage", "--now", "2026-10-15T08:30:00-05:00", "--new", "--workspace", str(ws))
    run2 = env["data"]["run"]
    done = run_agent(ws, choose=lambda q: next((o for o in q["options"] if o["id"] == "done"), q["options"][0]))
    assert done.failures == [] and done.final["kind"] == "done", done.final
    (rel,) = [o for o in plan_ops(ws, run2) if o.get("key", "").startswith("dk_s_")]
    assert rel["verb"] == "release" and "standing" in rel["reason"]
    assert not any(e["key"].startswith("dk_s_") for e in events(ics))
