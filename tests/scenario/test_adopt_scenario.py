"""Task lines without a key are adopted (TD §11.5, §12.3, §13.8). Other tools and the operator
add open lines to a task list that know nothing of the ledger. A destination with ``adopt``
gives each such line in its own sections its key through the plan: the operator sees the
adoption in the diff, approves it, and apply writes it like any replace. The block planned
for the line already carries the identity the adopted line has, so the next run keeps it.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from docket.items.add import key_of
from docket.store.paths import state_root
from tests.agent_loop import run_agent

NOW = "2026-10-15T08:00:00+00:00"          # a Thursday
ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Invented//Fixture//EN\r\nEND:VCALENDAR\r\n"
DENTIST = "- [ ] Call the dentist. [est:: 15m]"
TALK = "- [ ] Watch the storage talk [video](https://video.example.org/v/1) [due:: 2026-10-16] [est:: 45m]"
TASKS = f"""# Tasks

## Inbox
- [ ] Review the runbook [id:: iseed0000]
{DENTIST}
{TALK}
- [x] Renew the passport

## Priority

## Someday
- [ ] Learn the cello
"""
DENTIST_KEY = key_of("Call the dentist.", None)
TALK_KEY = key_of("Watch the storage talk", "https://video.example.org/v/1")
# due is required and missing: its default (+2d, working days from Thursday) is written before the key
DENTIST_ADOPTED = f"{DENTIST} [due:: 2026-10-19] [id:: {DENTIST_KEY}]"
TALK_ADOPTED = f"{TALK} [id:: {TALK_KEY}]"
ADOPTED = TASKS.replace(DENTIST + "\n", DENTIST_ADOPTED + "\n").replace(TALK + "\n", TALK_ADOPTED + "\n")

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
    adopt: true
    line_template: "- [ ] {action}{?url} [{source_label}]({url}){/url}{?due} [due:: {due}]{/due}{?est} [est:: {est|duration}]{/est}"
    quality:
      imperative: { deny: [fyi, maybe], default_verb: "Follow up on" }
      require: [link, due, est, key]
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


def workspace(root: Path, config: str = CONFIG, tasks: str = TASKS) -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "inbox.jsonl").write_text(json.dumps(ITEM) + "\n", encoding="utf-8")
    (root / "docket.yaml").write_text(config, encoding="utf-8")
    (root / "tasks.md").write_text(tasks, encoding="utf-8")
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


def dismiss(q: dict[str, Any]) -> dict[str, Any]:
    return next((o for o in q["options"] if o["id"] == "dismiss"), q["options"][0])


def at_approve(step: dict[str, Any]) -> bool:
    return step["kind"] == "ask" and step.get("node") == "approve"


def start(cli, ws: Path, *extra: str) -> None:
    code, env = cli("run", "start", "daily", "--now", NOW, "--workspace", str(ws), *extra)
    assert code == 0, env


def show(cli, ws: Path, key: str) -> dict[str, Any]:
    code, env = cli("ledger", "show", key, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["item"]


def operations(ws: Path, cli) -> list[dict[str, Any]]:
    code, env = cli("next", "--workspace", str(ws), "--full")
    assert code == 0, env
    [step] = [s for s in env["data"]["steps"] if s.get("node") == "approve"]
    q = step["questions"][0]
    plan = json.loads((Path(q["details"]["diff_path"]).parent / "plan.json").read_text(encoding="utf-8"))
    return plan["operations"]


def test_a_keyless_line_is_adopted_through_the_plan_and_its_block_is_kept_next_run(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    start(cli, ws)
    res = run_agent(ws, choose=dismiss, stop_at=at_approve)
    assert res.failures == [] and at_approve(res.final), (res.failures, res.final)
    ops = operations(ws, cli)
    adoptions = [o for o in ops if o.get("adopt")]
    assert [(o["old_line"], o["line"]) for o in adoptions] == [(DENTIST, DENTIST_ADOPTED), (TALK, TALK_ADOPTED)]
    assert all(o["verb"] == "replace" and o["destination"] == "backlog" for o in adoptions)
    # the block planned for the talk already carries the adopted line's identity
    blocks = [o for o in ops if o.get("destination") == "cal" and TALK_KEY in (o.get("task_ids") or [])]
    assert len(blocks) == 1 and blocks[0]["verb"] == "create", blocks
    code, text = cli("plan", "diff", "--workspace", str(ws), json_out=False)
    assert code == 0
    assert f"adopt `{TALK}` as {TALK_KEY}" in text and f"adopt `{DENTIST}` as {DENTIST_KEY}" in text, text
    assert (ws / "tasks.md").read_text() == TASKS                  # nothing is written before approval
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert (ws / "tasks.md").read_text() == ADOPTED
    row = show(cli, ws, TALK_KEY)
    assert (row["state"], row["source"], row["title"], row["url"]) == \
        ("scheduled", "hand", "Watch the storage talk", "https://video.example.org/v/1")
    assert (row["due"], row["est"]) == ("2026-10-16", 45)
    assert [(h["decision"], h.get("note")) for h in row["history"]][0] == ("adopted", "adopted from backlog")
    lines = [p for p in row["placements"] if p["adapter"] == "markdown_tasks"]
    assert [(p["destination"], p["path"], p["anchor"], p["line"]) for p in lines] == \
        [("backlog", "tasks.md", "## Inbox", TALK_ADOPTED)]
    assert [p for p in row["placements"] if p["adapter"] == "calendar_file"], row
    assert show(cli, ws, DENTIST_KEY)["due"] == "2026-10-19"
    ics = (ws / "cal.ics").read_text()

    # the second run finds the keys: it adopts nothing and keeps the block
    start(cli, ws, "--new")
    res = run_agent(ws, choose=dismiss, stop_at=at_approve)
    assert res.failures == [] and at_approve(res.final), (res.failures, res.final)
    ops = operations(ws, cli)
    assert [o for o in ops if o.get("adopt") or o.get("destination") == "backlog"] == []
    blocks = [o for o in ops if o.get("destination") == "cal" and TALK_KEY in (o.get("task_ids") or [])]
    assert [b["verb"] for b in blocks] == ["keep"], blocks
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert (ws / "tasks.md").read_text() == ADOPTED and (ws / "cal.ics").read_text() == ics
    assert show(cli, ws, TALK_KEY)["state"] == "scheduled"


def test_a_plan_that_schedules_nothing_adopts_and_the_item_waits_in_backlog(tmp_path, cli):
    plain = CONFIG.replace("    plan: { plan: [decide], schedule_tasks: true }\n", "    plan: { plan: [decide] }\n")
    ws = workspace(tmp_path / "ws", plain)
    start(cli, ws)
    res = run_agent(ws, choose=dismiss, stop_at=at_approve)
    assert res.failures == [] and at_approve(res.final), (res.failures, res.final)
    diff = Path(res.final["questions"][0]["details"]["diff_path"]).read_text(encoding="utf-8")
    assert f"adopt `{TALK}` as {TALK_KEY}" in diff, diff
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert (ws / "tasks.md").read_text() == ADOPTED
    assert show(cli, ws, TALK_KEY)["state"] == "backlog" and show(cli, ws, DENTIST_KEY)["state"] == "backlog"


def test_without_adopt_the_lines_are_left_alone(tmp_path, cli):
    ws = workspace(tmp_path / "ws", CONFIG.replace("    adopt: true\n", ""))
    start(cli, ws)
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert (ws / "tasks.md").read_text() == TASKS
    assert not any(r[0] in (TALK_KEY, DENTIST_KEY) for r in ledger_rows(ws))


def test_a_sandbox_run_adopts_nothing_outside_the_run(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    before = (ws / "tasks.md").read_bytes()
    start(cli, ws, "--sandbox")
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert (ws / "tasks.md").read_bytes() == before
    assert not any(r[0] in (TALK_KEY, DENTIST_KEY) for r in ledger_rows(ws))


def test_after_item_add_nothing_is_adopted(tmp_path, cli):
    ws = workspace(tmp_path / "ws", tasks=TASKS.replace(DENTIST + "\n", "").replace(TALK + "\n", ""))
    code, env = cli("item", "add", "Watch the storage talk", "--link", "https://video.example.org/v/1",
                    "--label", "video", "--due", "2026-10-16", "--est", "45m", "--now", NOW, "--workspace", str(ws))
    assert code == 0 and env["data"]["key"] == TALK_KEY, env
    start(cli, ws)
    res = run_agent(ws, choose=dismiss, stop_at=at_approve)
    assert res.failures == [] and at_approve(res.final), (res.failures, res.final)
    assert [o for o in operations(ws, cli) if o.get("adopt")] == []


def test_after_adoption_item_add_reports_the_existing_item(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    start(cli, ws)
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    before = (ws / "tasks.md").read_bytes()
    code, env = cli("item", "add", "Watch the storage  talk", "--link", "https://video.example.org/v/1",
                    "--now", NOW, "--workspace", str(ws))
    assert code == 0, env
    d = env["data"]
    assert (d["key"], d["changed"], d["line"]) == (TALK_KEY, False, TALK_ADOPTED)
    assert (ws / "tasks.md").read_bytes() == before


def test_a_line_for_a_done_item_reopens_it(tmp_path, cli):
    """The operator once added the task and later closed it in the ledger; now another tool
    wrote it again without a key. The open line is the evidence: the row goes back to backlog
    (and is scheduled with its block) and its history says an open line was found."""
    ws = workspace(tmp_path / "ws", tasks=TASKS.replace(DENTIST + "\n", ""))
    code, env = cli("item", "add", "Watch the storage talk", "--link", "https://video.example.org/v/1",
                    "--to", "backlog", "--now", NOW, "--workspace", str(ws))
    assert code == 0, env
    assert cli("ledger", "edit", TALK_KEY, "--state", "done", "--workspace", str(ws))[0] == 0
    (ws / "tasks.md").write_text(TASKS.replace(DENTIST + "\n", ""), encoding="utf-8")   # the keyed line is gone
    start(cli, ws)
    res = run_agent(ws, choose=dismiss, stop_at=at_approve)
    assert res.failures == [] and at_approve(res.final), (res.failures, res.final)
    diff = Path(res.final["questions"][0]["details"]["diff_path"]).read_text(encoding="utf-8")
    assert f"adopt `{TALK}` as {TALK_KEY}" in diff and "reopens" in diff, diff
    res = run_agent(ws, choose=dismiss)
    assert res.failures == [] and res.final["kind"] == "done", (res.failures, res.final)
    assert (ws / "tasks.md").read_text() == TASKS.replace(DENTIST + "\n", "").replace(TALK, TALK_ADOPTED)
    row = show(cli, ws, TALK_KEY)
    assert row["state"] == "scheduled"
    notes = [h.get("note") or "" for h in row["history"]]
    assert "an open line was found in backlog; reopened from done" in notes, row["history"]


def test_ledger_reconcile_lists_the_lines_the_next_plan_would_adopt(tmp_path, cli):
    ws = workspace(tmp_path / "ws")
    before = (ws / "tasks.md").read_bytes()
    for extra in (["--dry-run"], []):
        code, env = cli("ledger", "reconcile", *extra, "--workspace", str(ws))
        assert code == 0, env
        rows = env["data"]["adoptable"]
        assert [(r["destination"], r["section"], r["line"], r["key"]) for r in rows] == \
            [("backlog", "## Inbox", DENTIST, DENTIST_KEY), ("backlog", "## Inbox", TALK, TALK_KEY)]
        # the due default counts from today, which is not the pinned run date
        assert rows[0]["adopted"].startswith(DENTIST + " [due:: ") and rows[0]["adopted"].endswith(f" [id:: {DENTIST_KEY}]")
        assert rows[1]["adopted"] == TALK_ADOPTED
    code, text = cli("ledger", "reconcile", "--workspace", str(ws), json_out=False)
    assert code == 0 and "2 task line(s) without a key; the next plan adopts them:" in text, text
    assert (ws / "tasks.md").read_bytes() == before and ledger_rows(ws) == []
