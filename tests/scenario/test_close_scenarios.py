"""Approve, apply and close scenarios (IP M8 exit, TP §6.2; TD §5.4, §13).

Every hook here runs ``tests/fixtures/bin/hook_stub.py``, which writes only to the
paths it is given under the test's temporary directory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from docket.apply import executor
from docket.store.paths import state_root
from tests.agent_loop import run_agent

STUB = Path(__file__).resolve().parents[1] / "fixtures" / "bin" / "hook_stub.py"


def db_rows(ws: Path, sql: str, *args) -> list[tuple]:
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def run_dir(ws: Path, run_id: str) -> Path:
    return state_root(ws, os.environ) / "runs" / run_id


def start(cli, ws: Path, workflow: str, now: str, *extra: str) -> str:
    code, env = cli("run", "start", workflow, "--now", now, "--workspace", str(ws), *extra)
    assert code == 0, env
    return env["data"]["run"]


def append_yaml(ws: Path, text: str) -> None:
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text() + "\n" + text.strip("\n") + "\n")


def hook_yaml(hid: str, *args: str, events=("after_close",), policy="warn", env=(), dry_run=None) -> str:
    cmd = json.dumps([sys.executable, str(STUB), *args])
    out = f"  {hid}:\n    command: {cmd}\n    events: {json.dumps(list(events))}\n    on_failure: {policy}\n"
    if env:
        out += f"    env: {json.dumps(list(env))}\n"
    if dry_run is not None:
        out += f"    dry_run: {json.dumps([sys.executable, str(STUB), *dry_run])}\n"
    return out


# --- executor: crash after every operation ------------------------------------------------


@pytest.mark.parametrize("name,workflow,n_ops", [("reference-like", "triage", 8), ("support-lead", "shift", 12)])
def test_crash_after_verify_of_each_operation_resumes_without_duplicates(persona, cli, pinned_now, monkeypatch,
                                                                         name, workflow, n_ops):
    ws = persona(name)
    start(cli, ws, workflow, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "apply")
    assert res.failures == []
    crashed: set[str] = set()

    def crash(op):
        if op.op_id not in crashed:
            crashed.add(op.op_id)
            raise RuntimeError(f"injected crash after verify of {op.op_id}")

    monkeypatch.setattr(executor, "AFTER_VERIFY", crash)
    argv = res.final["command"]["argv"][1:]
    codes = []
    for _ in range(n_ops + 3):
        code, env = cli(*argv)
        codes.append(code)
        if code == 0:
            break
    assert len(crashed) == n_ops and codes.count(70) == n_ops and codes[-1] == 0
    monkeypatch.setattr(executor, "AFTER_VERIFY", None)
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"
    store = ws / ("Backlog.md" if name == "reference-like" else "worklog/todo.md")
    lines = [ln for ln in store.read_text().splitlines() if ln.startswith(("- [ ]", "TODO"))]
    assert len(lines) == len(set(lines))  # no line written twice
    rows = db_rows(ws, "SELECT outcome, verify_status FROM receipts WHERE destination IN ('backlog', 'worklog')")
    assert len(rows) == n_ops and {v for _, v in rows} == {"verified"} and {o for o, _ in rows} == {"applied"}


# --- approval and freshness ---------------------------------------------------------------

PLANNING = """
planning:
  workflows: [shift]
  calendar: { provider: json_file, path: data/calendar.json, calendar: work, destination: calendar }
  tasks: { destination: worklog }
"""
CAL_DEST = """
  calendar:
    adapter: calendar_file
    path: cal.ics
    calendar: work
"""
ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Invented//Fixture//EN\r\n{events}END:VCALENDAR\r\n"
EVENT = ("BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART:20261020T{h}0000Z\r\n"
         "DTEND:20261020T{h}3000Z\r\nEND:VEVENT\r\n")


def with_calendar(ws: Path) -> Path:
    cfg = ws / "docket.yaml"
    text = cfg.read_text().replace("\ndestinations:\n", "\ndestinations:\n" + CAL_DEST.lstrip("\n"), 1)
    cfg.write_text(text + PLANNING)
    ics = ws / "cal.ics"
    ics.write_text(ICS.format(events=""))
    return ics


def test_calendar_change_between_build_and_approval_refuses_with_the_rebuild_command(persona, cli, pinned_now):
    ws = persona("support-lead")
    ics = with_calendar(ws)
    run_id = start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    assert res.failures == [], res.failures
    q = res.final["questions"][0]
    old = q["details"]["digest"]
    assert set(q["details"]["counts"]) >= {"calendar", "worklog"} and q["details"]["diff_path"].endswith("diff.md")
    plan = json.loads((run_dir(ws, run_id) / "plan" / old / "plan.json").read_text())
    assert set(plan["inputs"]["destinations"]) == {"calendar", "worklog"}

    # someone adds two meetings to the calendar after the plan was built
    ics.write_text(ICS.format(events=EVENT.format(uid="m1", title="Vendor call", h="10") +
                              EVENT.format(uid="m2", title="Team retro", h="14")))
    code, env = cli("ask", "answer", "approve", "yes", "--digest", old, "--workspace", str(ws))
    assert code == 4 and env["error"]["code"] == "apply.stale_inputs", env
    change = env["error"]["details"]["destinations"]
    assert list(change) == ["calendar"] and change["calendar"]["summary"] == "2 calendar events added since build"
    assert env["next"]["command"][1:3] == ["plan", "build"]
    assert "approve" not in json.loads((run_dir(ws, run_id) / "answers.json").read_text())

    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["step"]["kind"] == "run" and env["data"]["step"]["command"]["argv"][1:3] == ["plan", "build"]
    code, env = cli("plan", "build", "--workspace", str(ws))
    assert code == 0, env
    q2 = env["data"]["step"]["questions"][0]
    new = q2["details"]["digest"]
    assert new != old
    code, env = cli("ask", "answer", "approve", "yes", "--digest", old, "--workspace", str(ws))
    assert code == 4 and env["error"]["code"] == "approve.digest_mismatch"
    code, env = cli("ask", "answer", "approve", "yes", "--digest", new, "--workspace", str(ws))
    assert code == 0 and env["data"]["step"]["command"]["argv"][1] == "apply", env
    rec = json.loads((run_dir(ws, run_id) / "answers.json").read_text())["approve"]
    assert rec["digest"] == new and set(rec["destinations"]) == {"calendar", "worklog"}
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done", done.final
    assert "[docket:" in ics.read_text() and "Vendor call" in ics.read_text()  # owned blocks beside the meetings


def test_unchanged_destinations_approve_after_any_delay(persona, cli, pinned_now):
    """No clock check: an approval long after the build passes when nothing changed (TD §13.2)."""
    ws = persona("support-lead")
    run_id = start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    pd = res.final["questions"][0]["details"]["digest"]
    plan_json = run_dir(ws, run_id) / "plan" / pd / "plan.json"
    os.utime(plan_json, (1, 1))  # built "decades" ago
    code, env = cli("ask", "answer", "approve", "yes", "--digest", pd, "--workspace", str(ws))
    assert code == 0, env


# --- the apply boundary -------------------------------------------------------------------


def test_upstream_change_after_apply_waits_with_post_apply_change(persona, cli, pinned_now):
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == []
    append_yaml(ws, "planning:\n  policy: { granularity: 30m }")  # a plan-stage section changes after apply
    code, env = cli("next", "--workspace", str(ws))
    step = env["data"]["step"]
    assert step["kind"] == "wait" and step["reason"] == "post_apply_change" and step["stage"] == "plan"
    inspect = [c["argv"][1:3] for c in step["inspect"]]
    assert ["run", "status"] in inspect and ["plan", "diff"] in inspect
    assert step["command"]["argv"][1:3] == ["run", "continue"]
    st = {s["stage"]: s for s in env["data"]["stages"]}
    assert st["plan"]["state"] == "current" and st["apply"]["state"] == "current"  # the plan is not staled
    code, env = cli("close", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "stage.post_apply_change"
    assert env["next"]["command"][1:3] == ["run", "continue"]
    code, env = cli("plan", "diff", "--workspace", str(ws))
    assert code == 0  # the approved plan is still inspectable
    code, env = cli("run", "continue", "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] is True
    assert env["data"]["step"]["kind"] == "run" and env["data"]["step"]["stage"] == "close"
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"


# --- cursors ------------------------------------------------------------------------------


def test_abandoned_run_advances_nothing_and_the_next_run_rereads(persona, cli, pinned_now):
    ws = persona("support-lead")
    run_id = start(cli, ws, "shift", pinned_now)
    code, env = cli("collect", "--workspace", str(ws))
    first = env["data"]["result"]["sources"]["tickets"]["records"]
    assert db_rows(ws, "SELECT COUNT(*) FROM cursor_proposals WHERE run_id=?", run_id)[0][0] > 0
    code, env = cli("run", "abandon", run_id, "--workspace", str(ws))
    assert code == 0
    assert db_rows(ws, "SELECT COUNT(*) FROM cursors")[0][0] == 0
    start(cli, ws, "shift", "2026-10-16T18:00:00+01:00", "--date", "2026-10-17")
    code, env = cli("collect", "--workspace", str(ws))
    assert env["data"]["result"]["sources"]["tickets"]["records"] == first  # re-read from the start


def test_sandbox_run_leaves_cursors_and_dispositions_untouched(persona, cli, pinned_now):
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now)
    assert run_agent(ws).final["kind"] == "done"
    cursors = db_rows(ws, "SELECT * FROM cursors ORDER BY source, unit")
    disp = db_rows(ws, "SELECT * FROM dispositions ORDER BY topic_id")
    assert cursors and disp
    with open(ws / "data" / "tickets.jsonl", "a") as fh:
        fh.write('{"key": "SUP-2100", "summary": "Sandbox-only ticket", "description": "d", '
                 '"requester": {"email": "pat@customer-one.example"}, "queue": "tier1", "priority": "P1", '
                 '"created": "2026-10-16T09:00:00Z"}\n')
    run_id = start(cli, ws, "shift", "2026-10-16T18:00:00+01:00", "--date", "2026-10-17", "--sandbox")
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    assert db_rows(ws, "SELECT * FROM cursors ORDER BY source, unit") == cursors
    assert db_rows(ws, "SELECT * FROM dispositions ORDER BY topic_id") == disp
    art = json.loads(next((run_dir(ws, run_id) / "artifacts" / "close").glob("*.json")).read_text())["data"]
    assert art["cursors_advanced"] == [] and "sandbox" in art["cursors_skipped"]


# --- hooks --------------------------------------------------------------------------------


def test_hook_failure_under_warn_does_not_block_close_and_is_not_repeated(persona, cli, pinned_now, tmp_path,
                                                                          monkeypatch):
    ws = persona("support-lead")
    count = tmp_path / "hook-count.txt"
    append_yaml(ws, "hooks:\n" + hook_yaml("notify", "--count", str(count), "--label", "notify", "--exit", "3")
                + hook_yaml("archive", "--count", str(count), "--label", "archive"))
    run_id = start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    from docket.engine.stages import STAGE_IMPLS
    impl = STAGE_IMPLS["close"]
    real = impl.finish
    calls = {"n": 0}

    def crash_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash after the close steps, before the artifact")
        return real(*a, **kw)

    monkeypatch.setattr(impl, "finish", crash_once)
    argv = res.final["command"]["argv"][1:]
    code, env = cli(*argv)
    assert code == 70  # crashed after hooks ran
    lines = count.read_text().splitlines()
    assert sorted(ln.split()[0] for ln in lines) == ["archive", "notify"]
    code, env = cli(*argv)  # resume
    assert code == 0, env
    assert len(count.read_text().splitlines()) == 2  # neither hook ran again
    hooks = {h["id"]: h for h in env["data"]["result"]["hooks"]}
    assert hooks["notify"]["outcome"] == "warned" and hooks["notify"]["exit"] == 3 and hooks["notify"]["repeated"]
    assert hooks["archive"]["outcome"] == "ok"
    assert env["data"]["step"]["kind"] == "done" and env["data"]["step"]["summary"]["status"] == "sealed"
    log = run_dir(ws, run_id) / "logs" / "hooks" / "after_close-notify.log"
    assert "stub hook notify ran" in log.read_text()
    # state steps did not repeat either: one ledger row set, one journal receipt
    steps = db_rows(ws, "SELECT step, outcome FROM step_receipts WHERE run_id=? ORDER BY step", run_id)
    assert ("close:state", "ok") in steps and ("hook:after_close:notify", "warned") in steps


def test_hook_failure_under_fail_stops_close_and_retries_only_that_hook(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    count, flag = tmp_path / "count.txt", tmp_path / "ready.flag"
    append_yaml(ws, "hooks:\n" + hook_yaml("commit", "--count", str(count), "--label", "commit",
                                           "--fail-unless", str(flag), policy="fail")
                + hook_yaml("index", "--count", str(count), "--label", "index", "--exit", "1"))
    start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    argv = res.final["command"]["argv"][1:]
    code, env = cli(*argv)
    assert code == 5 and env["error"]["code"] == "hook.failed" and env["error"]["details"]["hook"] == "commit"
    assert env["next"]["command"][1] == "close"
    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["step"]["kind"] == "run" and env["data"]["step"]["stage"] == "close"
    flag.write_text("ok")
    code, env = cli(*argv)
    assert code == 0, env
    labels = [ln.split()[0] for ln in count.read_text().splitlines()]
    assert labels == ["commit", "commit", "index"]  # commit retried; index ran once, after commit


def test_hook_environment_is_scrubbed_and_stdin_closed(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    count, envout = tmp_path / "count.txt", tmp_path / "env.txt"
    append_yaml(ws, "hooks:\n" + hook_yaml("reindex", "--count", str(count), "--env-out", str(envout),
                                           "--run", "{run.id}", env=["DOCKET_TEST_INDEX_DIR"]))
    run_id = start(cli, ws, "shift", pinned_now)
    extra = {"DOCKET_TEST_INDEX_DIR": "/tmp/invented-index", "VAULT_API_TOKEN": "invented-not-a-secret",
             "UNDECLARED_PLAIN": "x"}
    res = run_agent(ws, env={**os.environ, **extra})
    assert res.failures == [] and res.final["kind"] == "done"
    seen = dict(ln.split("=", 1) for ln in envout.read_text().splitlines() if not ln.startswith("#"))
    assert seen["DOCKET_TEST_INDEX_DIR"] == "/tmp/invented-index"
    assert "VAULT_API_TOKEN" not in seen and "UNDECLARED_PLAIN" not in seen
    assert seen["DOCKET_NONINTERACTIVE"] == "1" and seen["DOCKET_RUN_ID"] == run_id
    assert "invented-not-a-secret" not in envout.read_text()
    assert "#stdin_tty=False stdin_bytes=0" in envout.read_text() and "#controlling_tty=False" in envout.read_text()
    assert count.read_text().split() == ["hook", "--run", run_id]  # argv templates over run facts


def test_a_hook_that_hangs_times_out_and_reports_the_command(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    count = tmp_path / "count.txt"
    (ws / "scripts").mkdir()
    append_yaml(ws, "hooks:\n" + hook_yaml("slow", "--count", str(count), "--sleep", "20")
                + "    timeout_s: 0.5\n    cwd: scripts\n")
    start(cli, ws, "shift", pinned_now)
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    [row] = db_rows(ws, "SELECT outcome, json FROM step_receipts WHERE step='hook:after_close:slow'")
    body = json.loads(row[1])
    assert row[0] == "warned" and body["exit"] is None and "timed out after" in body["message"]
    assert str(STUB) in body["message"] and body["elapsed_s"] < 10


def test_hooks_run_once_per_event_even_when_commands_repeat(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    count = tmp_path / "count.txt"
    append_yaml(ws, "hooks:\n" + hook_yaml("collected", "--count", str(count), "--label", "collected",
                                           events=("after_collect",))
                + hook_yaml("applied", "--count", str(count), "--label", "applied", events=("after_apply",))
                + hook_yaml("closed", "--count", str(count), "--label", "closed", events=("after_close",)))
    start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, repeat_commands=True)
    assert res.failures == [] and res.final["kind"] == "done"
    assert [ln.split()[0] for ln in count.read_text().splitlines()] == ["collected", "applied", "closed"]


def test_close_dry_run_runs_only_declared_dry_run_forms(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    count = tmp_path / "count.txt"
    append_yaml(ws, "hooks:\n" + hook_yaml("commit", "--count", str(count), "--label", "commit",
                                           dry_run=["--count", str(count), "--label", "commit-dry"])
                + hook_yaml("reindex", "--count", str(count), "--label", "reindex"))
    run_id = start(cli, ws, "shift", pinned_now)
    run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    code, env = cli("close", "--dry-run", "--workspace", str(ws))
    assert code == 0 and env["data"]["dry_run"] is True
    hooks = {h["id"]: h for h in env["data"]["preview"]["hooks"]}
    assert hooks["commit"]["outcome"] == "ok" and hooks["commit"]["dry_run"] is True
    assert hooks["reindex"]["outcome"] == "skipped"
    assert [ln.split()[0] for ln in count.read_text().splitlines()] == ["commit-dry"]
    assert db_rows(ws, "SELECT COUNT(*) FROM step_receipts WHERE run_id=?", run_id)[0][0] == 0
    assert db_rows(ws, "SELECT COUNT(*) FROM cursors")[0][0] == 0


# --- close resumes without repeating a step ------------------------------------------------


def test_close_resumes_after_a_crash_between_steps_without_repeating(persona, cli, pinned_now, monkeypatch):
    ws = persona("reference-like")
    run_id = start(cli, ws, "triage", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    from docket.close import run as close_run
    real = close_run.journal_step
    state = {"n": 0}

    def crash_once(rc, st):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("injected crash after the state step")
        return real(rc, st)

    monkeypatch.setattr(close_run, "journal_step", crash_once)
    argv = res.final["command"]["argv"][1:]
    assert cli(*argv)[0] == 70
    ledger_rows = db_rows(ws, "SELECT COUNT(*) FROM ledgers WHERE run_id=? AND ledger='surfaced'", run_id)[0][0]
    assert ledger_rows == 8
    code, env = cli(*argv)
    assert code == 0, env
    assert db_rows(ws, "SELECT COUNT(*) FROM ledgers WHERE run_id=? AND ledger='surfaced'", run_id)[0][0] == 8
    assert cli(*argv)[0] == 0  # close again: current, no-op
    journal = (ws / "Daylog.md").read_text()
    assert journal.count(f"## 2026-10-15 {run_id}") == 1


# --- journal and dispositions ---------------------------------------------------------------


def test_journal_sections_render_for_both_personas(persona, cli, pinned_now):
    ws = persona("reference-like")
    run_id = start(cli, ws, "daily", pinned_now)
    res = run_agent(ws)
    assert res.final["kind"] == "done"
    text = open(res.final["summary"]["journal"]).read()
    assert text.startswith(f"# Daily journal 2026-10-15 (daily)\n")
    for heading in ("## Sources", "## Asked", "## Written", "## Not asked", "## Audit"):
        assert heading in text
    assert "15 item(s) surfaced across recent runs; 15 written today." in text
    assert "Approved " in text and "after the plan was built." in text  # approval_delay_note
    entry = (ws / "Daylog.md").read_text()
    assert entry.startswith(f"## 2026-10-15 {run_id}\n### Sources\n") and "### Not asked" in entry
    assert "# Daily journal" not in entry  # the title belongs to journal.md only

    ws2 = persona("support-lead", under="sl")
    start(cli, ws2, "shift", pinned_now)
    res = run_agent(ws2)
    text = open(res.final["summary"]["journal"]).read()
    assert text.startswith("# Shift log 2026-10-16\n")
    assert "## Queues read\n- pager: read, 5 record(s)\n- tickets: read, 30 record(s)" in text
    assert "## Decisions\n- ref:ticket:SUP-" in text and ": routed to worklog" in text
    assert "## Taken\n- TODO " in text


def test_a_dismissed_topic_is_suppressed_next_run_under_a_new_item_id(persona, cli, pinned_now):
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now)
    first = {}

    def choose(q):
        if not first:
            first["q"] = q
            return next(o for o in q["options"] if o["id"] == "close")
        return q["options"][0]

    res = run_agent(ws, choose=choose)
    assert res.failures == [] and res.final["kind"] == "done"
    item_id = first["q"]["id"][2:]
    code, env = cli("item", "list", "--workspace", str(ws), "--run", res.final["summary"]["run"])
    topic = next(it["topic"] for it in env["data"]["items"] if it["id"] == item_id)
    assert topic.startswith("ref:ticket:")
    rows = db_rows(ws, "SELECT decision, item FROM dispositions WHERE topic_id=?", topic)
    assert rows == [("dismiss", item_id)]
    routed = db_rows(ws, "SELECT COUNT(*) FROM dispositions WHERE decision='routed' AND destination='worklog' "
                         "AND op_id LIKE 'op_%'")[0][0]
    assert routed >= 1

    ref = topic.split(":", 2)[2]
    routed_topic = db_rows(ws, "SELECT topic_id FROM dispositions WHERE decision='routed' ORDER BY topic_id")[0][0]
    routed_ref = routed_topic.split(":", 2)[2]
    with open(ws / "data" / "tickets.jsonl", "a") as fh:
        for key, about in (("SUP-2200", ref), ("SUP-2201", routed_ref)):
            fh.write(json.dumps({"key": key, "summary": f"Customer asks again about {about}",
                                 "description": f"Same problem as {about}.",
                                 "requester": {"email": "pat@customer-one.example"},
                                 "queue": "tier1", "priority": "P2", "created": "2026-10-16T09:00:00Z"}) + "\n")
    start(cli, ws, "shift", "2026-10-16T18:00:00+01:00", "--date", "2026-10-17")
    res = run_agent(ws, stop_at=lambda s: s["kind"] in ("ask", "done", "wait"))
    code, env = cli("item", "list", "--workspace", str(ws))
    new = [it for it in env["data"]["items"] if it["topic"] == topic]
    assert len(new) == 1 and new[0]["id"] != item_id
    assert new[0]["status"] == "suppressed" and new[0]["reason"].startswith("disposition: dismiss")
    again = [it for it in env["data"]["items"] if it["topic"] == routed_topic]
    assert len(again) == 1 and again[0]["status"] != "suppressed"  # routed re-surfaces on new activity


def test_message_drafts_flow_through_apply_and_there_is_no_send(persona, cli, pinned_now, monkeypatch):
    from docket.cli.errors import IntegrityError  # noqa: F401
    from docket.config.models import Destination as DestConfig
    from docket.destinations.base import Action, DestEnv, make_op
    from docket.destinations.mcp import MemoryDrafts, MessageMcp
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now)
    run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "apply")
    from tests.scenario.test_apply_destinations import run_context
    rc = run_context(ws)
    cfg = DestConfig.model_validate({"adapter": "message_mcp", "tools": {"list": "l", "draft": "d"},
                                     "title_template": "{title}", "notes_template": "{note}"})
    store = MemoryDrafts()
    dest = MessageMcp("mail", cfg, DestEnv(root=ws), call=store, twin=True)
    rc.ws.config.destinations["mail"] = cfg
    monkeypatch.setattr(executor, "for_run", lambda rc_, did: dest)
    [op] = dest.preview(Action(kind="message.draft", destination="mail", key="q1",
                               fields={"title": "Refund status", "note": "Your refund is on its way.",
                                       "to": ["pat@customer-one.example"]}), dest.snapshot())
    assert op.verb == "draft" and "send" not in dest.verbs
    rows = executor.execute(rc, [op.dump()])
    assert rows[0]["outcome"] == "applied" and rows[0]["verify_status"] == "verified"
    assert len(store.drafts) == 1
    assert executor.execute(rc, [op.dump()])[0]["outcome"] == "applied"  # verified receipt: not drafted twice
    assert len(store.drafts) == 1
    send = make_op("mail", "send", ident="k", key="k", subject="x", body="y")
    rows = executor.execute(rc, [send.dump()])
    assert rows[0]["outcome"] == "refused" and rows[0]["error"]["code"] == "dest.unsupported"
