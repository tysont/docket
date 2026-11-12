"""``docket ledger edit <key>``: change one ledger item by hand. Only the attributes given
change, the values go through Docket's parsers, and the result says which placements now
differ from the row."""
from __future__ import annotations

import json


def imported(persona, cli, tmp_path):
    ws = persona("support-lead")
    f = tmp_path / "items.json"
    f.write_text(json.dumps([
        {"title": "Draft the refund runbook", "key": "iaaaaaaaa", "destination": "worklog", "adapter": "markdown_tasks",
         "est": "1h", "area": "Billing"},
        {"title": "Review the queue rota", "key": "ibbbbbbbb"},
    ]))
    code, env = cli("ledger", "import", str(f), "--workspace", str(ws))
    assert code == 0 and env["data"]["imported"] == 2, env
    return ws


def test_edit_changes_only_what_is_given_and_shows_before_and_after(persona, cli, tmp_path):
    ws = imported(persona, cli, tmp_path)
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--state", "monitoring", "--due", "2026-10-23", "--est", "1h30m",
                    "--note", "waiting on the billing team", "--dry-run", "--workspace", str(ws))
    assert code == 0, env
    d = env["data"]
    assert d["dry_run"] is True and d["key"] == "iaaaaaaaa"
    assert d["changes"] == {"state": ["backlog", "monitoring"], "due": [None, "2026-10-23"], "est": [60, 90]}
    assert d["before"]["state"] == "backlog" and d["after"]["state"] == "monitoring" and d["after"]["area"] == "Billing"
    assert d["placements"] == [{"destination": "worklog", "adapter": "markdown_tasks", "run": d["placements"][0]["run"]}]
    assert d["placements_changed"] is False
    show = cli("ledger", "show", "iaaaaaaaa", "--workspace", str(ws))[1]["data"]["item"]
    assert show["state"] == "backlog"                                       # a dry run writes nothing
    code, text = cli("ledger", "edit", "iaaaaaaaa", "--state", "monitoring", "--due", "2026-10-23", "--est", "1h30m",
                     "--note", "waiting on the billing team", "--workspace", str(ws), json_out=False)
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "edited iaaaaaaaa: Draft the refund runbook"
    assert any(ln.split() == ["state", "backlog", "->", "monitoring"] for ln in lines)
    assert any(ln.split() == ["est", "1h", "->", "1h30m"] for ln in lines)
    assert any(ln.split() == ["due", "-", "->", "2026-10-23"] for ln in lines)
    assert any("not changed: worklog (markdown_tasks)" in ln for ln in lines)
    show = cli("ledger", "show", "iaaaaaaaa", "--workspace", str(ws))[1]["data"]["item"]
    assert (show["state"], show["due"], show["est"], show["area"]) == ("monitoring", "2026-10-23", 90, "Billing")
    last = show["history"][-1]
    assert last["by"] == "hand" and last["decision"] == "edited" and last["note"] == "waiting on the billing team"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--no-due", "--title", "Draft the refund runbook v2",
                    "--workspace", str(ws))
    assert code == 0 and env["data"]["changes"] == {"due": ["2026-10-23", None],
                                                    "title": ["Draft the refund runbook", "Draft the refund runbook v2"]}


def test_edit_refuses_unknown_keys_bad_values_and_changes_that_change_nothing(persona, cli, tmp_path):
    ws = imported(persona, cli, tmp_path)
    code, env = cli("ledger", "edit", "iaaaaaaab", "--state", "done", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_item"
    assert env["error"]["details"]["suggestion"] == "iaaaaaaaa"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--state", "finished", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"
    assert "done" in env["error"]["details"]["states"]
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--due", "someday", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_date"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--est", "a while", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_duration"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--due", "2026-10-23", "--no-due", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--title", "  ", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--state", "backlog", "--area", "Billing", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "ledger.no_change"
    code, env = cli("ledger", "edit", "iaaaaaaaa", "--note", "only a note", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "ledger.no_change"
    code, env = cli("ledger", "edit", "ibbbbbbbb", "--state", "done", "--workspace", str(ws))
    assert code == 0 and env["data"]["placements"] == []
    text = cli("ledger", "edit", "ibbbbbbbb", "--state", "ignored", "--dry-run", "--workspace", str(ws),
               json_out=False)[1]
    assert text.splitlines()[0] == "would edit ibbbbbbbb: Review the queue rota" and "not changed" not in text
