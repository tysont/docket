"""Planning reads the task store through its destination adapter (TD §1 principle 5, §11.3; FR-PLN-05).

The support-lead persona's worklog uses a delimited grammar (``TODO ... | k=v``), not
checkbox lines. Run 1 takes findings into the worklog and plans a block per task on an
ICS calendar destination. Run 2, a new run before the work window opens, reads the same worklog and calendar:
every block of run 1 that is still ahead belongs to a still-open task and is kept, never
released, recreated or duplicated. A block placed with ``--when`` carries its window in the
line, so run 2 keeps it inside that window.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from docket.store.paths import state_root
from tests.agent_loop import run_agent

ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Invented//Fixture//EN\r\nEND:VCALENDAR\r\n"
MARKER = "(planned by docket: {key})"
RUN2 = "2026-10-16T07:00:00+01:00"  # the same Friday in Lisbon, before the work window opens


def setup(ws: Path, *, line_template: str | None = None) -> Path:
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    text = text.replace("destinations:\n  worklog:\n",
                        "destinations:\n"
                        f"  shift-calendar:\n    adapter: calendar_file\n    path: cal.ics\n"
                        f"    calendar: work\n    marker: \"{MARKER}\"\n"
                        "  worklog:\n", 1)
    if line_template is not None:
        text = re.sub(r'(?m)^    line_template: ".*"$', f'    line_template: "{line_template}"', text, count=1)
    text += ("\nplanning:\n  workflows: [shift]\n"
             "  calendar: { provider: destination, destination: shift-calendar, calendar: work }\n"
             "  tasks: { destination: worklog }\n")
    cfg.write_text(text)
    ics = ws / "cal.ics"
    ics.write_text(ICS)
    return ics


def owned(ics: Path) -> list[str]:
    return re.findall(r"\(planned by docket: ([A-Za-z0-9_-]+)\)", ics.read_text().replace("\r\n ", ""))


def plan_ops(ws: Path, run_id: str) -> list[dict]:
    rdir = state_root(ws, os.environ) / "runs" / run_id
    latest = (rdir / "artifacts" / "plan" / "latest").read_text().strip()
    return json.loads((rdir / "artifacts" / "plan" / f"{latest}.json").read_text())["data"]["operations"]


def second_run(cli, ws: Path) -> list[dict]:
    code, env = cli("run", "start", "shift", "--now", RUN2, "--new", "--workspace", str(ws))
    assert code == 0 and env["data"]["created"] is True, env
    run2 = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    return [o for o in plan_ops(ws, run2) if o.get("key")]


def test_a_delimited_store_keeps_run_one_blocks_on_run_two(persona, cli, pinned_now):
    ws = persona("support-lead")
    ics = setup(ws)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    run1 = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    todo = (ws / "worklog" / "todo.md").read_text()
    assert todo.count("\nTODO ") + todo.startswith("TODO ") >= 2, todo
    created1 = {o["key"]: o for o in plan_ops(ws, run1) if o.get("verb") == "create"}
    assert created1 and set(created1) == set(owned(ics))

    ops2 = second_run(cli, ws)
    ahead = {k for k, o in created1.items() if datetime.fromisoformat(o["start"]) >= datetime.fromisoformat(RUN2)}
    assert ahead, "run 1 placed blocks after run 2 starts"
    verbs = {o["key"]: o["verb"] for o in ops2 if o["key"] in created1}
    assert {k: verbs.get(k) for k in ahead} == {k: "keep" for k in ahead}, ops2
    per_key = Counter(o["key"] for o in ops2)
    assert set(per_key.values()) == {1}, per_key
    assert not [o for o in ops2 if o["verb"] in ("create", "release")], ops2
    keys2 = owned(ics)
    assert sorted(keys2) == sorted(created1)  # no duplicate and no loss after run 2 applied


def test_a_windowed_block_is_kept_on_run_two(persona, cli, pinned_now):
    ws = persona("support-lead")
    ics = setup(ws, line_template="TODO {title|truncate:70} | queue={fields.queue} | prio={fields.priority}"
                                  "{?when} | when={when}{/when}{?est} | est={est|duration}{/est}{?due} | due={due}{/due}")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    run1 = env["data"]["run"]
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "take", "--when", "mon 15:00-17:00", "--est", "60",
                    "--title", "Call the customer back", "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws, choose=lambda q: next(o for o in q["options"] if o["id"] == "watch")
                    if q.get("kind") == "finding" else q["options"][0])
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert "| when=2026-10-19 15:00-17:00" in (ws / "worklog" / "todo.md").read_text()
    created1 = [o for o in plan_ops(ws, run1) if o.get("verb") == "create"]
    assert len(created1) == 1 and created1[0]["start"].startswith("2026-10-19T15:00"), created1
    key = created1[0]["key"]

    ops2 = second_run(cli, ws)
    mine = [o for o in ops2 if o["key"] == key]
    assert [(o["verb"], o["start"]) for o in mine] == [("keep", created1[0]["start"])], ops2
    assert owned(ics) == [key]


def test_the_inline_fields_preset_line_carries_the_window(persona, cli, pinned_now):
    """The ``markdown/tasks-inline-fields`` preset writes ``[when:: ...]``, so run 2 keeps a windowed block."""
    ws = persona("reference-like")
    dests = ws / "destinations.yaml"
    text = re.sub(r"(?m)^    # The preset's line.*\n    line_template: .*\n", "", dests.read_text())
    assert "line_template" not in text.split("  agendas:")[0]  # the backlog uses the preset's line
    dests.write_text(text + f"  work-calendar:\n    adapter: calendar_file\n    path: cal.ics\n"
                            f"    calendar: work\n    marker: \"{MARKER}\"\n")
    plan = ws / "planning.yaml"
    plan.write_text(plan.read_text().replace("workflows: [week-plan]", "workflows: [triage]").replace(
        "calendar: { provider: json_file, path: data/calendar.json, calendar: work, destination: work-calendar }",
        "calendar: { provider: destination, destination: work-calendar, calendar: work }"))
    ics = ws / "cal.ics"
    ics.write_text(ICS)
    code, env = cli("run", "start", "triage", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    run1 = env["data"]["run"]
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "task", "--when", "mon 15:00-17:00", "--est", "60",
                    "--title", "Draft the offsite memo", "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws, choose=lambda q: next(o for o in q["options"] if o["id"] == "watch")
                    if q.get("kind") == "finding" else q["options"][0])
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert "[when:: 2026-10-19 15:00-17:00]" in (ws / "Backlog.md").read_text()
    created1 = [o for o in plan_ops(ws, run1) if o.get("verb") == "create" and o["start"].startswith("2026-10-19")]
    assert len(created1) == 1 and created1[0]["start"] == "2026-10-19T15:00:00-05:00", created1
    key = created1[0]["key"]

    code, env = cli("run", "start", "triage", "--now", "2026-10-16T07:00:00-05:00", "--workspace", str(ws))
    assert code == 0 and env["data"]["created"] is True, env
    run2 = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    mine = [(o["verb"], o["start"]) for o in plan_ops(ws, run2) if o.get("key") == key]
    assert mine == [("keep", created1[0]["start"])], mine
    assert owned(ics).count(key) == 1
