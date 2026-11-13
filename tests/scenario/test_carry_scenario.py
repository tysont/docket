"""A carried task moves its old block (TD §10.4, §12.3, FR-PLN-10).

Day 1 a run places blocks on a calendar destination and seals. Day 2 the blocks have
elapsed; reconcile asks about each and the operator says the work did not happen. The
task is placed again, and since its block's key derives from the task, the plan moves the
elapsed block to the new slot (``move`` with the old time in ``from``) instead of asking the
calendar for a second block under a key it already holds. Apply succeeds, and the calendar
holds one event per key at the new time. Run once against an ICS file (``calendar_file``)
and once against a calendar behind the exec protocol (the bridge stand-in). All data is
invented.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from docket.store.paths import state_root
from tests.agent_loop import fill_template, run_agent

STUB = Path(__file__).resolve().parents[1] / "fixtures" / "bin" / "calbridge_stub.py"
DAY1 = "2026-10-15T08:00:00-05:00"     # a Thursday, before the day window opens
DAY2 = "2026-10-16T09:10:00-05:00"     # the next morning: Thursday's blocks have elapsed
MARKER = "(planned by docket: {key})"
TASKS = ("- [ ] Draft the capacity memo [source:: seed] [est:: 3h]\n"
         "- [ ] Renew the build certificate [source:: seed] [est:: 10m]\n"
         "- [ ] Reply to the venue [source:: seed] [est:: 10m]\n")


def setup(ws: Path, kind: str, tmp_path: Path, *, provider: str = "destination") -> Path:
    """The triage graph places tasks on a calendar destination of ``kind``; planning reads that
    destination, or with ``provider: json_file`` the persona's calendar fixture."""
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    triage = ("    decide: { ask: basic, from: [merge], questions: { kinds: [finding], budget: 8, overflow: drop } }\n"
              "    plan: { plan: [decide] }\n")
    assert triage in text
    text = text.replace(triage, "    decide: { ask: basic, from: [merge], questions: { kinds: [reconcile, finding], "
                                "budget: 8, overflow: drop } }\n    plan: { plan: [decide], schedule_tasks: true }\n", 1)
    cfg.write_text(text)
    dests = ws / "destinations.yaml"
    if kind == "ics":
        state = ws / "cal.ics"
        state.write_text("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Invented//Fixture//EN\r\nEND:VCALENDAR\r\n")
        dests.write_text(dests.read_text() + f"  work-calendar:\n    adapter: calendar_file\n    path: cal.ics\n"
                                             f"    calendar: work\n    marker: \"{MARKER}\"\n")
    else:
        state = tmp_path / "bridge-state.json"
        dests.write_text(dests.read_text() + f"  work-calendar:\n    adapter: calendar_exec\n    calendar: work\n"
                                             f"    marker: \"{MARKER}\"\n"
                                             f"    program: [{sys.executable}, {STUB}, --state, {state}]\n")
    plan = ws / "planning.yaml"
    text = plan.read_text().replace("    horizon_days: 10\n", "    horizon_days: 10\n    split_over: 2h\n")
    if provider == "destination":
        text = text.replace(
            "calendar: { provider: json_file, path: data/calendar.json, calendar: work, destination: work-calendar }",
            "calendar: { provider: destination, destination: work-calendar, calendar: work }")
    plan.write_text(text)
    bl = ws / "Backlog.md"
    bl.write_text(bl.read_text().replace("\n## Someday", "\n" + TASKS + "\n## Someday", 1))
    return state


def events(state: Path) -> list[dict]:
    """(key, start, end) of every owned event on the calendar."""
    if state.suffix == ".ics":
        text = state.read_bytes().decode("utf-8").replace("\r\n ", "")
        out = []
        for block in text.split("BEGIN:VEVENT")[1:]:
            m = re.search(r"\(planned by docket: ([A-Za-z0-9_-]+)\)", block)
            if m:
                start = re.search(r"DTSTART[^:]*:(\S+)", block).group(1)
                out.append({"key": m.group(1), "start": start})
        return out
    data = json.loads(state.read_text())
    rows = data.get("events") if isinstance(data, dict) else data
    out = []
    for e in rows if isinstance(rows, list) else list(data.values()):
        m = re.search(r"\(planned by docket: ([A-Za-z0-9_-]+)\)", e.get("notes") or e.get("description") or "")
        if m:
            out.append({"key": m.group(1), "start": e["start"]})
    return out


def plan_ops(ws: Path, run_id: str) -> list[dict]:
    rdir = state_root(ws, os.environ) / "runs" / run_id
    latest = (rdir / "artifacts" / "plan" / "latest").read_text().strip()
    return json.loads((rdir / "artifacts" / "plan" / f"{latest}.json").read_text())["data"]["operations"]


def norm(iso: str) -> str:
    from datetime import datetime, timezone
    if re.fullmatch(r"\d{8}T\d{6}Z", iso):
        return datetime.strptime(iso, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


@pytest.mark.parametrize("kind", ["ics", "exec"])
def test_a_carried_task_moves_its_elapsed_block_and_apply_succeeds(persona, cli, tmp_path, kind):
    ws = persona("reference-like")
    state = setup(ws, kind, tmp_path)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env.get("error")

    # day 1: blocks placed and applied, run sealed
    code, env = cli("run", "start", "triage", "--now", DAY1, "--workspace", str(ws))
    run1 = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    created = [o for o in plan_ops(ws, run1) if o.get("verb") == "create"]
    thursday = {o["key"]: o for o in created if o["start"].startswith("2026-10-15")}
    kinds = Counter(o["kind"] for o in thursday.values())
    assert kinds["batch"] == 1 and any("(1/2)" in o["title"] for o in thursday.values()), thursday

    # day 2: every Thursday block elapsed; the operator says none of it happened, and one
    # task needs only part of its time again
    code, env = cli("run", "start", "triage", "--now", DAY2, "--workspace", str(ws))
    assert code == 0, env
    run2 = env["data"]["run"]
    step = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True).final
    assert step["kind"] == "ask", step
    reconcile = [q for q in step["questions"] if q["kind"] == "reconcile"]
    assert {q["details"]["block_key"] for q in reconcile} == set(thursday)
    partly = next(q for q in reconcile if "/2)" not in q["prompt"] and not q["details"].get("batch_block"))
    for q in step["questions"]:
        if q["kind"] != "reconcile":
            opt = q["options"][0]["id"]
            argv = fill_template(step["answer"]["argv"], q["id"], opt)
        elif q is partly:
            argv = fill_template(step["answer"]["argv"], q["id"], "partly") + ["--est", "45m"]
        else:
            argv = fill_template(step["answer"]["argv"], q["id"], "not_done")
        code, env = cli(*argv[1:])
        assert code == 0, env
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done", done.final
    assert done.final["summary"]["unwritten"] == []

    ops = plan_ops(ws, run2)
    for key, old in thursday.items():
        mine = [o for o in ops if o.get("key") == key and not o.get("follows")]
        assert [o["verb"] for o in mine] == ["move"], (key, mine)
        assert mine[0]["from"]["start"] == old["start"]
        assert mine[0]["start"] > DAY2[:10]
    assert not any(o.get("verb") == "create" and o["key"] in thursday for o in ops)

    # the calendar holds one event per key, at the new time
    on_cal = events(state)
    per_key = Counter(e["key"] for e in on_cal)
    assert set(per_key.values()) == {1}, per_key
    moved = {o["key"]: o for o in ops if o.get("verb") == "move"}
    for e in on_cal:
        if e["key"] in thursday:
            assert norm(e["start"]) == norm(moved[e["key"]]["start"])


def test_a_batch_whose_members_changed_leaves_the_elapsed_block_as_history(persona, cli, tmp_path):
    ws = persona("reference-like")
    state = setup(ws, "ics", tmp_path)
    cli("run", "start", "triage", "--now", DAY1, "--workspace", str(ws))
    assert run_agent(ws).final["kind"] == "done"
    before = Counter(e["key"] for e in events(state))

    code, env = cli("run", "start", "triage", "--now", DAY2, "--workspace", str(ws))
    run2 = env["data"]["run"]
    step = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True).final
    batch_qs = [q for q in step["questions"] if q["kind"] == "reconcile" and q["details"].get("batch_block")]
    assert len(batch_qs) == 2
    old_key = batch_qs[0]["details"]["block_key"]
    for q in step["questions"]:
        opt = "done" if q is batch_qs[0] else ("not_done" if q["kind"] == "reconcile" else q["options"][0]["id"])
        code, env = cli(*fill_template(step["answer"]["argv"], q["id"], opt)[1:])
        assert code == 0, env
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done", done.final
    after = Counter(e["key"] for e in events(state))
    assert after[old_key] == 1 and before[old_key] == 1           # the old batch block stays where it was
    assert set(after.values()) == {1}
    assert not any(o.get("key") == old_key for o in plan_ops(ws, run2))
