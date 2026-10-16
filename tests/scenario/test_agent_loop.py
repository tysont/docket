"""Agent loop scenarios (Test Plan §6.1): both personas reach done by looping on docket next."""
from __future__ import annotations

import os
import sqlite3

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent

CASES = [("reference-like", "daily"), ("support-lead", "shift")]


@pytest.mark.parametrize("name,workflow", CASES)
def test_agent_loop_reaches_done(persona, cli, pinned_now, name, workflow):
    ws = persona(name)
    code, env = cli("run", "start", workflow, "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws)
    assert res.failures == [], res.failures
    assert res.final["kind"] == "done", res.final
    summary = res.final["summary"]
    assert summary["status"] == "sealed"
    assert summary["questions"] > 0 and summary["answered"] == summary["questions"]
    assert summary["operations"] == summary["applied"] == summary["questions"]
    assert {"run", "dispatch", "ask", "done"} <= {s["kind"] for s in res.steps}
    assert os.path.isfile(summary["journal"])
    # every command the agent ran came from a step; nothing was templated
    assert all("<" not in a for argv in res.calls for a in argv)


def test_reference_daily_pauses_reviews_and_writes_backlog(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    paused = run_agent(ws, resume_paused=False)
    assert paused.failures == []
    assert paused.final["kind"] == "wait" and paused.final["reason"] == "paused"
    assert paused.final["command"]["argv"][1:3] == ["run", "resume"]
    dispatched = {t["kind"] for s in paused.steps if s["kind"] == "dispatch" for t in s["tasks"]}
    assert dispatched == {"assess", "review", "tone"}  # tone: the persona's workspace-defined kind
    # answering while paused refuses with the resume command
    code, env = cli("ask", "list", "--workspace", str(ws))
    qid = env["data"]["questions"][0]["id"]
    code, env = cli("ask", "answer", qid, "1", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "run.paused"
    assert env["next"]["command"][1:3] == ["run", "resume"]
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"
    assert done.final["summary"]["questions"] == 15  # the workflow's question budget
    backlog = (ws / "Backlog.md").read_text()
    inbox = backlog.split("## Inbox")[1].split("## Someday")[0]
    assert inbox.count("- [ ]") == 16 and "[source:: chat]" in inbox
    assert "## Someday\n- [ ] Read the storage tiering proposal" in backlog
    journal = open(done.final["summary"]["journal"]).read()
    assert "## Not asked" in journal and "transcripts: skipped" in journal


def test_triage_workflow_and_sandbox_write_nothing_live(persona, cli, pinned_now):
    ws = persona("reference-like")
    before = (ws / "Backlog.md").read_text()
    code, env = cli("run", "start", "triage", "--now", pinned_now, "--sandbox", "--workspace", str(ws))
    run_id = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    assert res.final["summary"]["questions"] == 8 and res.final["summary"]["sandbox"] is True
    assert (ws / "Backlog.md").read_text() == before
    twin = state_root(ws, os.environ) / "runs" / run_id / "sandbox" / "backlog" / "Backlog.md"
    assert twin.read_text().count("- [ ]") == before.count("- [ ]") + 8
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    assert db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0  # a sandbox run moves no cursor
    db.close()


def test_cursors_advance_at_close_and_next_run_reads_only_new(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert run_agent(ws).final["kind"] == "done"
    with open(ws / "data" / "tickets.jsonl", "a") as fh:
        fh.write('{"key": "SUP-2000", "summary": "New ticket after close", "description": "d", '
                 '"requester": {"email": "pat@customer-one.example"}, "queue": "tier1", "priority": "P2", '
                 '"created": "2026-10-16T09:00:00Z"}\n')
    code, env = cli("run", "start", "shift", "--now", "2026-10-16T18:00:00+01:00", "--date", "2026-10-17",
                    "--workspace", str(ws))
    assert env["data"]["created"] is True
    code, env = cli("collect", "--workspace", str(ws))
    assert env["data"]["result"]["sources"]["tickets"]["records"] == 1


def test_answer_overrides_echo_and_preview(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "take it", "--due", "monday", "--est", "1.5h",
                    "--when", "thu 13:00-17:00", "--title", "Call the customer back", "--workspace", str(ws))
    assert code == 0, env
    ans = env["data"]["answer"]
    assert ans["option"] == "take" and ans["overrides"]["due"] == "2026-10-19"  # run date is Fri 16th in Lisbon
    assert ans["overrides"]["est"] == 90 and ans["est_normalized"] == "1h30m"
    assert ans["overrides"]["when"] == "2026-10-22 13:00-17:00"
    assert env["data"]["preview"]["line"].startswith("TODO Call the customer back | queue=")
    assert env["data"]["preview"]["line"].endswith("| due=2026-10-19")
    code, env = cli("ask", "answer", q["id"], "e", "--workspace", str(ws))
    assert code == 0 and env["data"]["answer"]["option"] == "escalate"
    code, env = cli("ask", "answer", q["id"], "nope", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_option"
    code, env = cli("ask", "answer", q["id"], "1", "--due", "someday", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_date"
    code, env = cli("ask", "answer", q["id"], "1", "--est", "lots", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_duration"


def test_approval_is_bound_to_the_plan_digest(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    q = res.final["questions"][0]
    digest = q["details"]["digest"]
    code, env = cli("ask", "answer", "approve", "yes", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.missing_digest"
    code, env = cli("ask", "answer", "approve", "yes", "--digest", "0" * 64, "--workspace", str(ws))
    assert code == 4 and env["error"]["code"] == "approve.digest_mismatch"
    assert env["error"]["details"]["expected"] == digest
    code, env = cli("ask", "answer", "approve", "no", "--digest", digest, "--workspace", str(ws))
    assert code == 0 and env["data"]["step"]["kind"] == "wait" and env["data"]["step"]["reason"] == "plan_rejected"
    code, env = cli("ask", "answer", "approve", "yes", "--digest", digest, "--workspace", str(ws))
    assert code == 0 and env["data"]["step"]["command"]["argv"][1] == "apply"


def test_stage_command_out_of_order_is_not_ready(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("plan", "build", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "stage.not_ready"
    assert env["error"]["details"]["blocking_stage"] == "collect"
    assert env["next"]["command"][1] == "collect"
