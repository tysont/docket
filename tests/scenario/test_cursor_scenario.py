"""Cursors through the CLI over real runs: where each source stands after a seal, a reset
that makes the next run read a source again, and a reset refused while an open run has
already read the source (TD §13.4)."""
from __future__ import annotations

import json
import os

from docket.store.paths import state_root
from tests.agent_loop import run_agent


def entries(ws, run_id: str, sid: str) -> int:
    path = state_root(ws, os.environ) / "runs" / run_id / "read" / sid / "outcome.json"
    return json.loads(path.read_text())["entries"]


def test_a_reset_source_is_read_again_and_an_open_run_that_read_it_blocks_the_reset(persona, cli, pinned_now):
    ws = persona("reference-like")
    code, env = cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    first = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"

    code, env = cli("cursor", "list", "--workspace", str(ws))
    assert code == 0, env
    chat = next(s for s in env["data"]["sources"] if s["source"] == "chat")
    assert chat["model"] == "units" and chat["units"] >= 2 and chat["updated_run"] == first
    code, env = cli("cursor", "list", "--source", "chat", "--units", "--workspace", str(ws))
    listed = env["data"]["sources"][0]["cursors"]
    assert len(listed) == chat["units"] and all(c["name"] for c in listed)      # names from the unit registry

    code, env = cli("cursor", "reset", "chat", "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] == chat["units"], env
    chat = cli("cursor", "list", "--source", "chat", "--workspace", str(ws))[1]["data"]["sources"][0]
    assert chat["units"] == 0

    code, env = cli("run", "start", "daily", "--new", "--now", pinned_now, "--workspace", str(ws))
    second = env["data"]["run"]
    code, env = cli("do", "read-chat", "--workspace", str(ws))
    assert code == 0, env
    assert entries(ws, second, "chat") == entries(ws, first, "chat") > 0       # read again from the lookback

    code, env = cli("cursor", "reset", "chat", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "cursor.read_in_open_run" and second in env["error"]["message"]
    code, text = cli("cursor", "reset", "chat", "--workspace", str(ws), json_out=False)
    assert code == 7
    assert cli("run", "abandon", second, "--workspace", str(ws))[0] == 0
    code, env = cli("cursor", "reset", "chat", "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] == 0
