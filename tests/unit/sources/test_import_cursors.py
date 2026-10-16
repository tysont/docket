"""``docket import cursors --from <json>`` (TD §19): seed durable cursors once, forward only, all or nothing."""
from __future__ import annotations

import json
import os
import sqlite3

from docket.store.paths import state_root


def cursors(ws) -> dict:
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    try:
        return {(s, u): (p, c, r) for s, u, p, c, r in
                db.execute("SELECT source, unit, position, coverage_end, updated_run FROM cursors")}
    finally:
        db.close()


def test_import_writes_cursors_and_the_first_run_reads_only_what_is_new(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    f = tmp_path / "watermarks.json"
    f.write_text(json.dumps({"cursors": [
        {"source": "tickets", "position": "2026-10-15T00:00:00Z", "coverage_end": "2026-10-15T00:00:00Z"},
        {"source": "pager", "unit": "*", "position": 7},
    ]}))
    code, env = cli("import", "cursors", "--from", str(f), "--dry-run", "--workspace", str(ws))
    assert code == 0 and len(env["data"]["would_write"]) == 2
    assert not (state_root(ws, os.environ) / "docket.db").exists() or cursors(ws) == {}
    code, env = cli("import", "cursors", "--from", str(f), "--workspace", str(ws))
    assert code == 0 and len(env["data"]["written"]) == 2, env
    got = cursors(ws)
    assert got[("tickets", "*")] == ("2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z", "import:watermarks.json")
    assert got[("pager", "*")][0] == "7"
    code, env = cli("import", "cursors", "--from", str(f), "--workspace", str(ws))  # repeating changes nothing
    assert code == 0 and env["data"]["written"] == [] and len(env["data"]["unchanged"]) == 2


def test_mapping_form_and_problems_refuse_everything(persona, cli, tmp_path):
    ws = persona("support-lead")
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"tickets": "2026-10-10T00:00:00Z", "tikets": "1", "pager": {"*": {"position": None}}}))
    code, env = cli("import", "cursors", "--from", str(f), "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_cursor_file"
    probs = env["error"]["details"]["problems"]
    assert any("tikets: no such source (did you mean 'tickets'?)" in p for p in probs)
    assert any("position must be" in p for p in probs)
    f.write_text("not json")
    code, env = cli("import", "cursors", "--from", str(f), "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_json"
    f.write_text(json.dumps({"tickets": "2026-10-10T00:00:00Z"}))
    code, env = cli("import", "cursors", "--from", str(f), "--workspace", str(ws))
    assert code == 0 and cursors(ws)[("tickets", "*")][0] == "2026-10-10T00:00:00Z"
