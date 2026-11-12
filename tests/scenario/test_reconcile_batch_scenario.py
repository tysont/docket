"""Reconcile covers batch blocks (TD §10.4, FR-DEC-05): an elapsed block that held several small
tasks asks about each member still open, with the same options and operations as a task
block. The members are the ones the plan that placed the block named. All data is invented."""
from __future__ import annotations

import json
import os

from docket.store.paths import state_root
from tests.agent_loop import run_agent

REF_NOW = "2026-10-15T18:00:00-05:00"   # a Thursday
NEXT_DAY = "2026-10-16T18:00:00-05:00"
SMALL = ["Renew the build certificate", "Reply to the venue", "File the expense report"]


def start(cli, ws, now, *extra):
    code, env = cli("run", "start", "week-plan", "--now", now, "--workspace", str(ws), *extra)
    assert code == 0, env
    return env["data"]["run"]


def plans(ws, run_id):
    d = state_root(ws, os.environ) / "runs" / run_id / "plan"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*/plan.json"))]


def add_small_tasks(ws):
    bl = ws / "Backlog.md"
    lines = "".join(f"- [ ] {t} [source:: seed] [est:: 10m]\n" for t in SMALL)
    bl.write_text(bl.read_text().replace("\n## Someday", "\n" + lines + "\n## Someday", 1))


def the_batch(ws, run_id):
    """The batch block the first run's plan placed: its key and the task ids it holds."""
    for plan in plans(ws, run_id):
        for b in plan.get("schedule") or []:
            if b.get("kind") == "batch":
                return b["key"], b["task_ids"]
    raise AssertionError("the plan placed no batch block")


def elapsed_block(ws, key, start="2026-10-16T13:00:00", end="2026-10-16T13:30:00"):
    cal = ws / "data" / "calendar.json"
    data = json.loads(cal.read_text())
    data["events"].append({"id": "blk-batch", "calendar": "work", "title": "3 small tasks", "start": start, "end": end,
                           "docket_key": key})
    cal.write_text(json.dumps(data))


def batch_questions(step, key):
    return [q for q in step["questions"] if q["kind"] == "reconcile" and q["details"].get("block_key") == key]


def test_an_elapsed_batch_block_asks_about_each_open_member(persona, cli):
    ws = persona("reference-like")
    add_small_tasks(ws)
    first = start(cli, ws, REF_NOW)
    assert run_agent(ws).final["kind"] == "done"
    key, members = the_batch(ws, first)
    assert key.startswith("dk_b_") and len(members) == 3

    elapsed_block(ws, key)
    bl = ws / "Backlog.md"
    bl.write_text(bl.read_text().replace("- [ ] File the expense report", "- [x] File the expense report"))
    start(cli, ws, NEXT_DAY, "--new")
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    assert res.final["kind"] == "ask", res.final
    qs = batch_questions(res.final, key)
    assert sorted(q["prompt"] for q in qs) == [
        'Did "Renew the build certificate" happen in "3 small tasks" (Fri 2026-10-16 13:00-13:30)?',
        'Did "Reply to the venue" happen in "3 small tasks" (Fri 2026-10-16 13:00-13:30)?']   # closed: not asked
    assert len({q["id"] for q in qs}) == 2 and all(q["id"].startswith("r_" + key) for q in qs)
    for q in qs:
        assert [o["id"] for o in q["options"]] == ["done", "partly", "not_done", "to_agenda", "drop"]
        assert q["details"]["batch_block"] == {"key": key, "title": "3 small tasks", "members": len(qs)}
        assert q["details"]["task_id"] in members

    # the same operations as a task block: close, carry
    renew, venue = sorted(qs, key=lambda q: q["prompt"])
    code, env = cli("ask", "answer", renew["id"], "done", "--workspace", str(ws))
    assert code == 0, env
    assert [(o["verb"], o["old_line"], o["line"]) for o in env["data"]["action_preview"]["operations"]] == [
        ("replace", "- [ ] Renew the build certificate [source:: seed] [est:: 10m]",
         "- [x] Renew the build certificate [source:: seed] [est:: 10m]")]
    code, env = cli("ask", "answer", venue["id"], "not_done", "--workspace", str(ws))
    assert code == 0, env
    assert env["data"]["action_preview"]["operations"][0]["line"] == \
        "- [ ] Reply to the venue [source:: seed] [est:: 10m] [carry:: 1]"

    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done", done.final

    # a member is asked once: the next run's window starts after this one
    start(cli, ws, "2026-10-17T09:00:00-05:00", "--new")
    later = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    assert later.final["kind"] != "ask" or batch_questions(later.final, key) == []


def test_a_batch_block_whose_members_no_plan_named_is_not_asked(persona, cli):
    ws = persona("reference-like")
    add_small_tasks(ws)
    elapsed_block(ws, "dk_b_0123456789ab", start="2026-10-15T13:00:00", end="2026-10-15T13:30:00")
    start(cli, ws, REF_NOW)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    assert res.final["kind"] == "ask"
    assert batch_questions(res.final, "dk_b_0123456789ab") == []
