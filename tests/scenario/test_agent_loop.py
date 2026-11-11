"""Agent loop scenarios (Test Plan §6.1): both personas reach done by looping on docket next."""
from __future__ import annotations

import os
import sqlite3

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent

CASES = [("reference-like", "daily"), ("support-lead", "shift")]


@pytest.mark.parametrize("name,graph", CASES)
def test_agent_loop_reaches_done(persona, cli, pinned_now, name, graph):
    ws = persona(name)
    code, env = cli("run", "start", graph, "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws)
    assert res.failures == [], res.failures
    assert res.final["kind"] == "done", res.final
    summary = res.final["summary"]
    assert summary["status"] == "sealed"
    assert summary["questions"] > 0 and summary["answered"] == summary["questions"]
    assert summary["operations"] == summary["applied"] == summary["questions"]
    assert {"run", "dispatch", "ask"} <= {s["kind"] for s in res.steps}
    assert all(s["node"] for s in res.steps)  # every step names its node
    assert res.steps[-1] == {**res.steps[-1], "kind": "run", "node": "seal"}  # every graph ends with the seal
    assert os.path.isfile(summary["journal"])
    # every command the agent ran came from a step; nothing was templated
    assert all("<" not in a and "{" not in a for argv in res.calls for a in argv)


def test_reference_daily_reviews_and_writes_backlog(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"
    dispatched = {t["kind"] for s in done.steps if s["kind"] == "dispatch" for t in s["tasks"]}
    assert dispatched == {"assess", "review", "tone"}  # tone: the persona's workspace-defined kind
    assert done.final["summary"]["questions"] == 15  # the decide node's question budget
    backlog = (ws / "Backlog.md").read_text()
    inbox = backlog.split("## Inbox")[1].split("## Someday")[0]
    assert inbox.count("- [ ]") == 16 and "[source:: chat]" in inbox
    assert "## Someday\n- [ ] Read the storage tiering proposal" in backlog
    journal = open(done.final["summary"]["journal"]).read()
    assert "## Not asked" in journal and "transcripts: skipped" in journal


def test_independent_nodes_are_ready_together(persona, cli, pinned_now):
    """Nodes that do not depend on each other move in the same round: every read node at the
    start, then the assess and tone judges, which both work only on reads."""
    ws = persona("reference-like")
    code, env = cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    assert {(s["kind"], s["node"]) for s in env["data"]["steps"]} == \
        {("run", "read-chat"), ("run", "read-mail"), ("run", "read-transcripts")}
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "dispatch")
    code, env = cli("next", "--workspace", str(ws))
    assert {(s["kind"], s["node"]) for s in env["data"]["steps"]} == {("dispatch", "assess"), ("dispatch", "tone")}
    nodes = {n["node"]: n for n in env["data"]["nodes"]}
    assert nodes["merge"]["state"] == "blocked" and nodes["merge"]["needs"] == ["assess", "tone"]


def test_triage_graph_and_sandbox_write_nothing_live(persona, cli, pinned_now):
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


def test_cursors_advance_at_seal_and_next_run_reads_only_new(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert run_agent(ws).final["kind"] == "done"
    with open(ws / "data" / "tickets.jsonl", "a") as fh:
        fh.write('{"key": "SUP-2000", "summary": "New ticket after the seal", "description": "d", '
                 '"requester": {"email": "pat@customer-one.example"}, "queue": "tier1", "priority": "P2", '
                 '"created": "2026-10-16T09:00:00Z"}\n')
    code, env = cli("run", "start", "shift", "--now", "2026-10-16T18:00:00+01:00", "--date", "2026-10-17",
                    "--workspace", str(ws))
    assert env["data"]["created"] is True
    code, env = cli("do", "read-tickets", "--workspace", str(ws))
    assert code == 0 and env["data"]["result"]["sources"]["tickets"]["records"] == 1


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
    # the fixture's line writes when and est back, so the block stays in its window on the next run
    assert env["data"]["preview"]["line"].endswith("| when=2026-10-22 13:00-17:00 | est=1h30m | due=2026-10-19")
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
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["node"] == "approve")
    q = res.final["questions"][0]
    digest = q["details"]["digest"]
    code, env = cli("ask", "answer", "approve", "yes", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.missing_digest"
    code, env = cli("ask", "answer", "approve", "yes", "--digest", "0" * 64, "--workspace", str(ws))
    assert code == 4 and env["error"]["code"] == "approve.digest_mismatch"
    assert env["error"]["details"]["expected"] == digest
    code, env = cli("ask", "answer", "approve", "no", "--digest", digest, "--workspace", str(ws))
    assert code == 0
    assert [(s["kind"], s["node"], s.get("reason")) for s in env["data"]["steps"]] == \
        [("wait", "approve", "plan_rejected")]
    code, env = cli("ask", "answer", "approve", "yes", "--digest", digest, "--workspace", str(ws))
    assert code == 0
    (step,) = env["data"]["steps"]
    assert step["node"] == "apply" and step["command"]["argv"][1:3] == ["do", "apply"]


def test_a_node_that_is_not_ready_refuses_with_what_it_waits_for(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("do", "plan", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "node.not_ready"
    assert env["error"]["details"]["node"] == "plan" and env["error"]["details"]["needs"] == ["decide"]
    assert env["next"]["command"][1:3] == ["do", "read-tickets"]  # the first ready step
    code, env = cli("do", "nope", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "node.not_in_graph"
    code, env = cli("do", "assess", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "node.not_computed"  # a judge is done by task submit


def test_a_node_waits_only_for_what_it_names(persona, cli, pinned_now):
    """sla-check works on read-tickets alone: once tickets are read it is ready, while assess,
    which also works on read-pager, still waits."""
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("do", "read-tickets", "--workspace", str(ws))
    assert code == 0, env
    assert {(s["kind"], s["node"]) for s in env["data"]["steps"]} == {("run", "read-pager"), ("dispatch", "sla-check")}
    code, env = cli("next", "--workspace", str(ws))
    nodes = {n["node"]: n for n in env["data"]["nodes"]}
    assert nodes["read-tickets"]["state"] == "current"
    assert nodes["assess"]["state"] == "blocked" and nodes["assess"]["reason"] == "waiting on read-pager"
    assert nodes["seal"]["state"] == "blocked"
    code, env = cli("do", "read-tickets", "--workspace", str(ws))  # a current node is a no-op
    assert code == 0 and env["data"]["status"] == "current" and env["data"]["changed"] is False


def test_a_fresh_agent_with_only_help_completes_a_fixture_run(persona, cli, pinned_now):
    """IP M9 exit: an agent that has read nothing but ``docket help --json`` completes a run."""
    ws = persona("reference-like")
    code, env = cli("help", "--json")
    assert code == 0 and "docket next --json" in env["data"]["loop"]
    names = {c["name"]: c for c in env["data"]["commands"]}
    assert "next" in names and "run start" in names
    code, env = cli("next", "--workspace", str(ws))  # no run yet: done, with the command that starts one
    assert env["data"]["done"] is True and env["data"]["steps"] == []
    start = env["data"]["start"]
    assert start["argv"][1:3] == ["run", "start"]
    flags = {a["name"] for a in names["run start"]["arguments"]}
    assert "--now" in flags  # pinned clock, for a reproducible fixture run
    code, env = cli(*start["argv"][1:], "--now", pinned_now)
    assert code == 0 and env["data"]["created"] is True
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done" and res.final["summary"]["status"] == "sealed"
