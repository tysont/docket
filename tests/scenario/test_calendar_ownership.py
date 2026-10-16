"""One ownership scheme for calendar blocks (TD §11.2, principle 5; FR-PLN-05, FR-ACT-04, FR-DEC-03).

Run 1 plans and applies blocks to an ICS calendar destination. Run 2 reads the same
destination (``planning.calendar.provider: destination``): every block run 1 created is
found as owned through the destination's marker, gets keep, move or release (never a
second create), and the reconcile questions name those blocks.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from docket.store.paths import state_root
from tests.agent_loop import run_agent

ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Invented//Fixture//EN\r\n"
       "BEGIN:VEVENT\r\nUID:sync-1@fixture\r\nSUMMARY:Platform Sync\r\nDTSTART:20261016T150000Z\r\n"
       "DTEND:20261016T153000Z\r\nATTENDEE:mailto:sam@team.example\r\nEND:VEVENT\r\n"
       "END:VCALENDAR\r\n")
MARKER = "(planned by docket: {key})"


def setup(ws: Path) -> Path:
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    text = text.replace("    questions: { kinds: [finding], budget: 8 }",
                        "    questions: { kinds: [reconcile, finding], budget: 8 }", 1)
    cfg.write_text(text)
    dests = ws / "destinations.yaml"
    dests.write_text(dests.read_text() + f"  work-calendar:\n    adapter: calendar_file\n    path: cal.ics\n"
                                         f"    calendar: work\n    marker: \"{MARKER}\"\n")
    plan = ws / "planning.yaml"
    plan.write_text(plan.read_text().replace("workflows: [week-plan]", "workflows: [week-plan, triage]").replace(
        "calendar: { provider: json_file, path: data/calendar.json, calendar: work, destination: work-calendar }",
        "calendar: { provider: destination, destination: work-calendar, calendar: work }"))
    ics = ws / "cal.ics"
    ics.write_text(ICS)
    return ics


def owned(ics: Path) -> list[str]:
    return re.findall(r"\(planned by docket: ([A-Za-z0-9_-]+)\)", ics.read_text().replace("\r\n ", ""))


def plan_ops(ws: Path, run_id: str) -> list[dict]:
    rdir = state_root(ws, os.environ) / "runs" / run_id
    latest = (rdir / "artifacts" / "plan" / "latest").read_text().strip()
    return json.loads((rdir / "artifacts" / "plan" / f"{latest}.json").read_text())["data"]["operations"]


def test_blocks_applied_by_run_one_are_owned_by_run_two(persona, cli, pinned_now):
    ws = persona("reference-like")
    ics = setup(ws)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    code, env = cli("run", "start", "triage", "--now", pinned_now, "--workspace", str(ws))
    run1 = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    keys1 = owned(ics)
    assert keys1 and len(keys1) == len(set(keys1))
    assert "Platform Sync" in ics.read_text()  # the meeting is kept byte for byte beside the blocks
    created1 = {o["key"] for o in plan_ops(ws, run1) if o.get("verb") == "create"}
    assert created1 == set(keys1)

    # Run 2, a day later: the blocks of run 1 are read back through the same marker.
    code, env = cli("run", "start", "triage", "--now", "2026-10-16T18:00:00-05:00", "--workspace", str(ws))
    assert code == 0 and env["data"]["created"] is True, env
    run2 = env["data"]["run"]
    seen: list[dict] = []
    res2 = run_agent(ws, choose=lambda q: (seen.append(q), next(
        (o for o in q["options"] if o["id"] == "not_done"), q["options"][0]))[1])
    assert res2.failures == [] and res2.final["kind"] == "done", res2.final
    reconcile = [q for q in seen if q["kind"] == "reconcile"]
    assert reconcile, "run 2 asks about the elapsed blocks run 1 placed"
    assert {q["details"]["block_key"] for q in reconcile} <= set(keys1)
    ops2 = [o for o in plan_ops(ws, run2) if o.get("key")]
    for o in ops2:
        if o["key"] in keys1:
            assert o["verb"] in ("keep", "move", "release"), o  # never a duplicate create, nor a spurious update
    from collections import Counter
    per_key = Counter(o["key"] for o in ops2 if o["key"] in keys1)
    assert per_key and set(per_key.values()) == {1}, per_key  # each owned block in the horizon: one decision
    reconciled = {q["details"]["block_key"] for q in reconcile}
    assert set(per_key) | reconciled == set(keys1)  # every block of run 1 is planned or reconciled
    keys2 = owned(ics)
    assert len(keys2) == len(set(keys2))  # no duplicate block after run 2 applied
    assert not any(o["verb"] == "create" and o["key"] in keys1 for o in ops2)
