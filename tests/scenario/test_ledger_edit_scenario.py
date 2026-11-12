"""A hand edit of the item ledger after a real run: the row changes, the task line and the
calendar blocks it became do not, and the result names them (TD §13.8)."""
from __future__ import annotations

from tests.agent_loop import run_agent


def test_a_hand_edit_changes_the_row_and_names_the_placements_it_left_alone(persona, cli, pinned_now):
    from tests.scenario.test_calendar_ownership import setup
    ws = persona("reference-like")
    setup(ws)
    dst = ws / "destinations.yaml"
    dst.write_text(dst.read_text().replace('{/links}"', '{/links} [id:: {key}]"', 1))
    cli("run", "start", "triage", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    rows = cli("ledger", "list", "--state", "scheduled", "--workspace", str(ws))[1]["data"]["items"]
    assert rows
    it = rows[0]
    task_file = ws / next(p["path"] for p in it["placements"] if p["adapter"] == "markdown_tasks")
    calendar = ws / "cal.ics"
    before_files = (task_file.read_text(), calendar.read_text())

    code, env = cli("ledger", "edit", it["key"], "--state", "done", "--note", "finished in the meeting",
                    "--workspace", str(ws))
    assert code == 0, env
    d = env["data"]
    assert d["changes"] == {"state": ["scheduled", "done"]} and d["placements_changed"] is False
    assert {p["adapter"] for p in d["placements"]} == {p["adapter"] for p in it["placements"]}
    assert (task_file.read_text(), calendar.read_text()) == before_files       # the destinations are as they were
    code, text = cli("ledger", "edit", it["key"], "--title", "Renamed by hand", "--dry-run", "--workspace", str(ws),
                     json_out=False)
    assert "not changed: work-calendar (calendar" in text and "not changed:" in text.split("\n", 2)[-1]
    got = cli("ledger", "show", it["key"], "--workspace", str(ws))[1]["data"]["item"]
    assert got["state"] == "done" and got["title"] == it["title"]
    assert got["history"][-1]["by"] == "hand" and got["history"][-1]["note"] == "finished in the meeting"
    code, env = cli("ledger", "edit", it["key"][:-1] + ("a" if it["key"][-1] != "a" else "b"), "--state", "done", "--workspace", str(ws))
    assert code == 2 and env["error"]["details"]["suggestion"] == it["key"]
