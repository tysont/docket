"""A refused or failed operation holds the run (TD §5.2, §13.3, FR-APP-07).

An apply whose operations did not all complete is not done: ``docket next`` waits with
``apply_partial``, names each operation that did not complete with its error, and gives the
driver two commands: repeat what did not complete (``--again``), or accept that it stays
unwritten (``--accept-failures``). The seal refuses while a writing node holds. Accepting
records the operations in the run, and the seal summary and the journal show them. All data
is invented.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from docket.store.paths import state_root
from tests.agent_loop import run_agent
from tests.scenario.test_carry_scenario import DAY1, setup


def to_apply(persona, cli, tmp_path):
    ws = persona("reference-like")
    # planning reads the persona's calendar fixture, so a change to the ICS file after approval
    # does not stale the plan: it reaches only the apply
    ics = setup(ws, "ics", tmp_path, provider="json_file")
    code, env = cli("run", "start", "triage", "--now", DAY1, "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["node"] == "apply")
    assert res.failures == [] and res.final["node"] == "apply", res.final
    return ws, ics, res.final, env["data"]["run"]


def approved_ops(ws: Path, run_id: str) -> list[dict]:
    rdir = state_root(ws, os.environ) / "runs" / run_id
    latest = (rdir / "artifacts" / "approve" / "latest").read_text().strip()
    return json.loads((rdir / "artifacts" / "approve" / f"{latest}.json").read_text())["data"]["operations"]


def squat(ics: Path, key: str) -> None:
    """Another event carrying the key of a block apply is about to create, with other content:
    the calendar refuses the create (``dest.state_changed``)."""
    text = ics.read_bytes().decode("utf-8")
    ev = ("BEGIN:VEVENT\r\nUID:squat@fixture\r\nDTSTART:20261014T150000Z\r\nDTEND:20261014T153000Z\r\n"
          f"SUMMARY:Something else\r\nDESCRIPTION:(planned by docket: {key})\r\nEND:VEVENT\r\n")
    ics.write_bytes(text.replace("END:VCALENDAR", ev + "END:VCALENDAR").encode("utf-8"))


def test_a_refused_operation_holds_the_apply_until_repeated_or_accepted(persona, cli, tmp_path):
    ws, ics, step, run_id = to_apply(persona, cli, tmp_path)
    creates = [o for o in approved_ops(ws, run_id) if o.get("verb") == "create"]
    target = creates[0]
    squat(ics, target["key"])

    code, env = cli(*step["command"]["argv"][1:])
    assert code == 5 and env["error"]["code"] == "dest.apply_failed", env.get("error")
    assert env["error"]["retryable"] is False                          # a refusal does not pass on its own
    assert [r["op_id"] for r in env["error"]["details"]["incomplete"]] == [target["op_id"]]

    code, env = cli("next", "--workspace", str(ws))
    (wait,) = env["data"]["steps"]
    assert (wait["kind"], wait["node"], wait["reason"]) == ("wait", "apply", "apply_partial")
    (row,) = wait["operations"]
    assert row == {"op_id": target["op_id"], "destination": "work-calendar", "verb": "create",
                   "what": target["title"], "outcome": "refused", "code": "dest.state_changed",
                   "message": row["message"], "retryable": False}
    assert "already exists with different content" in row["message"]
    assert [c["argv"][1:4] for c in wait["commands"]] == [["do", "apply", "--again"],
                                                          ["do", "apply", "--accept-failures"]]
    assert not any(s["node"] == "seal" for s in env["data"]["steps"])

    code, env = cli("seal", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "seal.writes_incomplete", env
    assert env["error"]["details"]["nodes"] == ["apply"]
    assert "--accept-failures" in env["error"]["hint"] and "--again" in env["error"]["hint"]

    # repeating does not help while the calendar still holds the other event
    code, env = cli(*wait["commands"][0]["argv"][1:])
    assert code == 5 and env["error"]["code"] == "dest.apply_failed"

    code, env = cli(*wait["commands"][1]["argv"][1:])
    assert code == 0, env
    assert env["data"]["accepted"] == 1 and env["data"]["status"] == "current"
    hdr = json.loads((state_root(ws, os.environ) / "runs" / run_id / "run.json").read_text())
    assert [r["op_id"] for r in hdr["unwritten"]["apply"]["operations"]] == [target["op_id"]]

    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    (unwritten,) = res.final["summary"]["unwritten"]
    assert unwritten["op_id"] == target["op_id"] and unwritten["code"] == "dest.state_changed"
    journal = (state_root(ws, os.environ) / "runs" / run_id / "journal.md").read_text()
    section = journal.split("## Unwritten", 1)[1].split("\n## ", 1)[0]
    assert f"work-calendar: {target['title']} [refused: dest.state_changed]" in section


def test_repeating_after_the_cause_is_fixed_completes_the_apply(persona, cli, tmp_path):
    ws, ics, step, run_id = to_apply(persona, cli, tmp_path)
    target = next(o for o in approved_ops(ws, run_id) if o.get("verb") == "create")
    squat(ics, target["key"])
    code, env = cli(*step["command"]["argv"][1:])
    assert code == 5
    text = ics.read_bytes().decode("utf-8")
    ics.write_bytes(re.sub(r"BEGIN:VEVENT\r\nUID:squat@fixture.*?END:VEVENT\r\n", "", text, flags=re.S).encode())
    code, env = cli("do", "apply", "--again", "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    assert res.final["summary"]["unwritten"] == []


def test_accept_failures_applies_only_to_a_writing_node_that_holds(persona, cli, tmp_path):
    ws, ics, step, run_id = to_apply(persona, cli, tmp_path)
    code, env = cli("do", "plan", "--accept-failures", "--workspace", str(ws))
    assert code == 0 and env["data"]["status"] == "current"           # a current node: nothing to do
    code, env = cli("do", "apply", "--accept-failures", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "node.not_ready"     # not run yet: nothing to accept
    code, env = cli("do", "apply", "--again", "--accept-failures", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"
