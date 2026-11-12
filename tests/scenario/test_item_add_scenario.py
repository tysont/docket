"""A task added by hand (TD §13.8): ``docket item add`` writes one line to a private task
store, as the destination renders a triaged item's line, and enters the item in the ledger
in backlog. The next run schedules it like any other open task, and the ledger follows it.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from docket.store.paths import state_root
from tests.agent_loop import run_agent

NOW = "2026-10-15T08:00:00+00:00"          # a Thursday
ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Invented//Fixture//EN\r\nEND:VCALENDAR\r\n"
TASKS = "# Tasks\n\n## Inbox\n- [ ] Review the runbook [id:: iseed0000]\n\n## Priority\n"

CONFIG = """\
version: 1
profile:
  timezone: UTC
  me: { name: Sam Doe, handles: { mail: sam@example.org } }
  workdays: [mon, tue, wed, thu, fri]
sources:
  inbox:
    kind: files
    path: data/inbox.jsonl
    mapping: { id: "$.id", title: "$.title", text: "$.text", from: "$.from", at: "$.at" }
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
    section: "## Inbox"
    sections: { inbox: "## Inbox", priority: "## Priority" }
    line_template: "- [ ] {action}{?url} [{source_label}]({url}){/url}{?due} [due:: {due}]{/due}{?est} [est:: {est|duration}]{/est}{?area} [area:: {area}]{/area}"
    quality:
      imperative: { deny: [fyi, maybe], default_verb: "Follow up on" }
      require: [due, est, key]
      fields: { key: id }
      defaults: { est: 30m, due: "+2d" }
  cal:
    adapter: calendar_file
    path: cal.ics
    calendar: work
    marker: "(planned by docket: {key})"
planning:
  calendar: { provider: destination, destination: cal, calendar: work }
  tasks: { destination: backlog }
graphs:
  daily:
    gather: { read: inbox }
    assess: { judge: assess, from: [gather] }
    merge: { merge: [assess] }
    decide: { ask: default, from: [merge] }
    plan: { plan: [decide], schedule_tasks: true }
    approve: { approve: plan }
    apply: { apply: approve }
"""
ITEM = {"id": "m1", "title": "Send the quarterly report", "text": "Please send the quarterly report.",
        "from": "pat@example.org", "at": "2026-10-14T09:00:00Z"}


def workspace(root: Path, config: str = CONFIG) -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "inbox.jsonl").write_text(json.dumps(ITEM) + "\n", encoding="utf-8")
    (root / "docket.yaml").write_text(config, encoding="utf-8")
    (root / "tasks.md").write_text(TASKS, encoding="utf-8")
    (root / "cal.ics").write_text(ICS, encoding="utf-8")
    return root


def ledger_rows(ws: Path) -> list[tuple[Any, ...]]:
    db = state_root(ws, os.environ) / "docket.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT * FROM item_ledger ORDER BY key").fetchall()
    finally:
        conn.close()


def add(cli, ws: Path, *args: str, json_out: bool = True) -> tuple[int, Any]:
    return cli("item", "add", *args, "--now", NOW, "--workspace", str(ws), json_out=json_out)


def test_the_line_is_the_destinations_with_every_field_given(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    code, env = add(cli, ws, "Watch the storage talk", "--link", "https://video.example.org/v/1",
                    "--link", "https://notes.example.org/n/2", "--label", "video", "--due", "2026-10-20",
                    "--est", "45m", "--area", "Learning", "--tier", "priority", "--note", "recommended in the sync")
    assert code == 0, env
    d = env["data"]
    key = d["key"]
    line = (f"- [ ] Watch the storage talk [video](https://video.example.org/v/1) [due:: 2026-10-20] [est:: 45m] "
            f"[area:: Learning] [id:: {key}]")
    assert d["line"] == line and d["changed"] is True and d["dry_run"] is False
    assert (d["destination"], d["path"], d["section"]) == ("backlog", "tasks.md", "## Priority")
    assert (ws / "tasks.md").read_text() == TASKS + line + "\n"
    row = cli("ledger", "show", key, "--workspace", str(ws))[1]["data"]["item"]
    assert (row["state"], row["title"], row["url"], row["source"]) == ("backlog", "Watch the storage talk",
                                                                      "https://video.example.org/v/1", "hand")
    assert (row["due"], row["est"], row["area"]) == ("2026-10-20", 45, "Learning")
    assert row["history"][-1] | {"at": "-"} == {"run": "add-2026-10-15", "at": "-", "by": "hand",
                                                 "decision": "added", "state": "backlog",
                                                 "note": "recommended in the sync"}
    [p] = row["placements"]
    assert (p["destination"], p["adapter"], p["path"], p["anchor"], p["line"]) == \
        ("backlog", "markdown_tasks", "tasks.md", "## Priority", line)
    assert d["ledger"] == row
    code, text = add(cli, ws, "Watch the storage talk", "--link", "https://video.example.org/v/1", json_out=False)
    assert code == 0 and text.startswith(f"{key} is already open in backlog: tasks.md under ## Priority"), text


def test_quality_rules_fill_their_defaults_and_rewrite_a_denied_opening(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    code, text = add(cli, ws, "fyi the budget doc", json_out=False)
    assert code == 0, text
    lines = text.splitlines()
    key = lines[0].split()[1]
    # +2d counts working days from Thursday: Monday
    assert lines[:3] == [f"added {key} to backlog: tasks.md under ## Inbox",
                         f"  - [ ] Follow up on the budget doc [due:: 2026-10-19] [est:: 30m] [id:: {key}]",
                         f"  ledger: {key} backlog"]
    assert lines[3] == "unexpected shapes: 1 (docket unexpected)"      # the rewrite is recorded
    assert (ws / "tasks.md").read_text().count("Follow up on the budget doc") == 1


def test_the_same_task_twice_writes_one_line(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    first = add(cli, ws, "Call the dentist", "--due", "2026-10-16")[1]["data"]
    before = (ws / "tasks.md").read_bytes()
    code, env = add(cli, ws, "call the  dentist", "--due", "2026-10-21")
    assert code == 0, env
    d = env["data"]
    assert d["changed"] is False and d["key"] == first["key"] and d["line"] == first["line"]
    assert (ws / "tasks.md").read_bytes() == before
    assert len(ledger_rows(ws)) == 1


def test_an_open_item_whose_line_is_gone_is_written_again(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    first = add(cli, ws, "Call the dentist", "--due", "2026-10-16")[1]["data"]
    (ws / "tasks.md").write_text(TASKS)                                     # the operator deleted the line
    code, env = add(cli, ws, "Call the dentist", "--due", "2026-10-16")
    assert code == 0 and env["data"]["changed"] is True and env["data"]["line"] == first["line"], env
    assert (ws / "tasks.md").read_text() == TASKS.replace("\n\n## Priority", "\n" + first["line"] + "\n\n## Priority")
    row = env["data"]["ledger"]
    assert [h["decision"] for h in row["history"]] == ["added", "added"] and len(row["placements"]) == 1


def test_a_done_item_refuses_and_names_ledger_edit(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    key = add(cli, ws, "Call the dentist")[1]["data"]["key"]
    assert cli("ledger", "edit", key, "--state", "done", "--workspace", str(ws))[0] == 0
    before = (ws / "tasks.md").read_bytes()
    code, env = add(cli, ws, "Call the dentist")
    assert code == 7 and env["error"]["code"] == "item.closed", env
    assert "docket ledger edit" in env["error"]["message"] and env["error"]["hint"].startswith(f"docket ledger edit {key}")
    assert (ws / "tasks.md").read_bytes() == before


def test_a_shared_destination_refuses(tmp_path, cli):
    shared = CONFIG.replace("    path: tasks.md\n", "    path: tasks.md\n    visibility: shared\n", 1)
    ws = workspace(tmp_path / "ws", shared)
    code, env = add(cli, ws, "Call the dentist")
    assert code == 2 and env["error"]["code"] == "item.shared_destination", env
    assert "private" in env["error"]["message"] and env["error"]["details"]["destination"] == "backlog"
    assert (ws / "tasks.md").read_text() == TASKS and ledger_rows(ws) == []


def test_several_task_stores_without_to_refuse_and_list_them(tmp_path, cli):
    two = CONFIG.replace("  cal:\n", "  someday:\n    adapter: markdown_tasks\n    path: someday.md\n  cal:\n", 1)
    ws = workspace(tmp_path / "ws", two)
    code, env = add(cli, ws, "Call the dentist")
    assert code == 2 and env["error"]["code"] == "input.ambiguous_destination", env
    assert env["error"]["details"]["destinations"] == ["backlog", "someday"]
    assert "backlog, someday" in env["error"]["message"]
    code, env = add(cli, ws, "Call the dentist", "--to", "someday")
    assert code == 0 and env["data"]["destination"] == "someday", env
    assert (ws / "someday.md").read_text().startswith("## Inbox\n- [ ] Call the dentist")
    code, env = add(cli, ws, "Call the dentist", "--to", "backlog")       # open where it was put: not written twice
    assert code == 0 and env["data"]["changed"] is False and env["data"]["destination"] == "someday", env
    assert "Call the dentist" not in (ws / "tasks.md").read_text()
    code, env = add(cli, ws, "Call the dentist", "--to", "cal")
    assert code == 2 and env["error"]["code"] == "input.unknown_destination", env


def test_an_unknown_tier_refuses_and_lists_the_tiers(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    code, env = add(cli, ws, "Call the dentist", "--tier", "urgent")
    assert code == 2 and env["error"]["code"] == "input.unknown_tier", env
    assert env["error"]["details"]["tiers"] == ["inbox", "priority"]
    assert (ws / "tasks.md").read_text() == TASKS


def test_a_required_link_is_not_demanded_of_an_item_added_by_hand(tmp_path, cli):
    """A task list may require a link so that every triaged item leads back to its record. An
    item added by hand has no record: its own words are the evidence, and it is written."""
    linked = CONFIG.replace("require: [due, est, key]", "require: [link, due, est, key]", 1)
    ws = workspace(tmp_path / "ws", linked)
    code, env = add(cli, ws, "Call the dentist", "--est", "15m")
    assert code == 0 and env["data"]["changed"] is True, env
    assert "Call the dentist" in env["data"]["line"] and "](" not in env["data"]["line"]


def test_a_quality_refusal_gives_a_fix_that_is_an_item_add_command(tmp_path, cli):
    strict = CONFIG.replace("require: [due, est, key]", "require: [due, est, area, key]", 1)
    ws = workspace(tmp_path / "ws", strict)
    code, env = add(cli, ws, "Watch the storage talk", "--est", "45m")
    assert code == 4 and env["error"]["code"] == "dest.quality", env
    hint = env["error"]["hint"]
    assert hint == env["error"]["details"]["fix"]
    assert hint.startswith("docket item add 'Watch the storage talk' --est 45m --workspace ") and \
        hint.endswith(" --area '<area>'"), hint
    assert "ask answer" not in hint
    assert (ws / "tasks.md").read_text() == TASKS and ledger_rows(ws) == []


def test_a_dry_run_writes_nothing(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    add(cli, ws, "Review the plan")                               # the ledger exists and holds one row
    before = ((ws / "tasks.md").read_bytes(), ledger_rows(ws))
    code, env = add(cli, ws, "Watch the storage talk", "--link", "https://video.example.org/v/1", "--est", "1h",
                    "--dry-run")
    assert code == 0, env
    d = env["data"]
    assert d["dry_run"] is True and d["changed"] is True and d["outcome"] is None
    assert d["line"].startswith("- [ ] Watch the storage talk [link](https://video.example.org/v/1)")
    assert (d["destination"], d["path"], d["section"]) == ("backlog", "tasks.md", "## Inbox")
    assert d["ledger"]["state"] == "backlog" and d["ledger"]["est"] == 60 and d["ledger"]["placements"][0]["line"] == d["line"]
    assert ((ws / "tasks.md").read_bytes(), ledger_rows(ws)) == before
    code, text = add(cli, ws, "Watch the storage talk", "--dry-run", json_out=False)
    assert code == 0 and text.startswith("would add i"), text


def test_the_next_run_schedules_an_added_task_and_the_ledger_follows(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    code, env = add(cli, ws, "Watch the storage talk", "--link", "https://video.example.org/v/1",
                    "--due", "2026-10-16", "--est", "45m")
    assert code == 0, env
    key = env["data"]["key"]
    code, env = cli("run", "start", "daily", "--now", NOW, "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws, choose=lambda q: next((o for o in q["options"] if o["id"] == "dismiss"), q["options"][0]))
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    ics = (ws / "cal.ics").read_text().replace("\r\n ", "")
    assert "Watch the storage talk" in ics
    row = cli("ledger", "show", key, "--workspace", str(ws))[1]["data"]["item"]
    assert row["state"] == "scheduled", row
    blocks = [p for p in row["placements"] if p["adapter"] == "calendar_file"]
    assert len(blocks) == 1 and blocks[0]["start"].startswith("2026-10-1")
    assert [p["adapter"] for p in row["placements"] if p["adapter"] == "markdown_tasks"] == ["markdown_tasks"]
    assert (ws / "tasks.md").read_text().count("Watch the storage talk") == 1


def test_an_open_run_rebuilds_a_plan_the_added_task_made_stale(tmp_path, cli):
    """item add takes no lease and needs no run. A plan built before the add reads a store
    that has changed: the plan node is stale, approving the old plan refuses, and the
    rebuilt plan places the added task."""
    from tests.agent_loop import fill_template
    ws = workspace(tmp_path / "ws")
    assert cli("run", "start", "daily", "--now", NOW, "--workspace", str(ws))[0] == 0
    dismiss = lambda q: next((o for o in q["options"] if o["id"] == "dismiss"), q["options"][0])   # noqa: E731
    res = run_agent(ws, choose=dismiss, stop_at=lambda s: s["kind"] == "ask" and s["node"] == "approve")
    approve = res.final
    assert approve.get("node") == "approve", approve
    code, env = add(cli, ws, "Watch the storage talk", "--est", "45m")
    assert code == 0, env
    key = env["data"]["key"]
    steps = cli("next", "--workspace", str(ws))[1]["data"]["steps"]
    assert [(s["kind"], s["node"]) for s in steps] == [("run", "plan")]
    q = approve["questions"][0]
    code, env = cli(*fill_template(approve["answer"]["argv"], q["id"], "yes")[1:])
    assert code == 4 and env["error"]["code"] == "apply.stale_inputs", env
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert "Watch the storage talk" in (ws / "cal.ics").read_text()
    assert cli("ledger", "show", key, "--workspace", str(ws))[1]["data"]["item"]["state"] == "scheduled"
