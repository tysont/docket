"""A sandbox run writes nothing live (TD §14.3, FR-ROB-03, NFR-08, TP §8.1).

``docket run start <graph> --sandbox`` binds every destination of the run to its sandbox
twin under ``runs/<id>/sandbox/``. These tests hash everything a sandbox run could touch
(the workspace and its destination files, another run's directory, the durable tables of
the database) before and after a sandbox run driven to its seal, and require them unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent
from tests.scenario.test_seal_scenarios import STUB, add_nodes, run_node

# Tables a run reads across runs: a sandbox run must leave every row as it was.
DURABLE = ("cursors", "units", "processed_ids", "dispositions", "item_ledger", "ledgers", "task_labels", "receipts")


def digest_tree(p: Path) -> dict[str, str]:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p.rglob("*")) if f.is_file()}


def durable_rows(ws: Path) -> dict[str, list[tuple]]:
    """Every row of the durable tables."""
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    try:
        return {t: sorted(db.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in DURABLE}
    finally:
        db.close()


def snapshot(ws: Path, live_run: str) -> dict[str, object]:
    return {"workspace": digest_tree(ws), "live": digest_tree(state_root(ws, os.environ) / "runs" / live_run),
            "db": durable_rows(ws)}


def start(cli, ws: Path, graph: str, now: str, *extra: str) -> dict:
    code, env = cli("run", "start", graph, "--now", now, "--workspace", str(ws), *extra)
    assert code == 0, env
    return env["data"]


@pytest.mark.parametrize("name,graph,store", [("reference-like", "daily", "Backlog.md"),
                                              ("support-lead", "shift", "worklog/todo.md")])
def test_both_personas_run_a_sandbox_to_the_seal_writing_nothing_live(persona, cli, pinned_now, name, graph,
                                                                      store):
    ws = persona(name)
    live = start(cli, ws, graph, pinned_now)["run"]
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")  # a live run waits on the operator
    assert res.failures == [] and res.final["node"] == "decide"
    before = snapshot(ws, live)
    t0 = time.perf_counter()
    started = start(cli, ws, graph, pinned_now, "--sandbox")
    assert started["created"] is True and started["sandbox"] is True and started["run"] != live
    sandbox = started["run"]
    # once the sandbox run is sealed, the newest open run is the live one again: stop at its first step
    res = run_agent(ws, stop_at=lambda s: f'"--run", "{live}"' in json.dumps(s))
    assert res.failures == [] and time.perf_counter() - t0 < 60
    assert (res.final["kind"], res.final["node"]) == ("ask", "decide")  # the live run is still where it was
    code, env = cli("next", "--run", sandbox, "--workspace", str(ws))
    assert code == 0 and env["data"]["done"] is True
    summary = env["data"]["summary"]
    assert summary["sandbox"] is True and summary["status"] == "sealed"
    assert summary["operations"] and summary["applied"] == summary["operations"]
    assert isinstance(summary["unexpected"], int)
    assert snapshot(ws, live) == before  # nothing outside the sandbox run's own state changed
    sdir = state_root(ws, os.environ) / "runs" / sandbox
    twin = sdir / "sandbox" / ("backlog" if name == "reference-like" else "worklog") / store
    live_text = (ws / store).read_text() if (ws / store).is_file() else ""  # the worklog starts absent
    marker = "- [ ]" if name == "reference-like" else "TODO "
    assert twin.read_text().count(marker) == live_text.count(marker) + summary["applied"]  # the twin took every write
    assert Path(summary["journal"]).is_file() and Path(summary["journal"]).resolve().is_relative_to(sdir.resolve())
    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["run"] == live and [(s["kind"], s["node"]) for s in env["data"]["steps"]] == [("ask", "decide")]
    # The live run then plans the very same operations. The sandbox's receipts are its own: the
    # live run must write every one of them for real, not take them as already done.
    res = run_agent(ws)
    assert res.failures == []
    code, env = cli("next", "--run", live, "--workspace", str(ws))
    assert code == 0 and env["data"]["done"] is True
    done = env["data"]["summary"]
    assert done["sandbox"] is False and done["applied"] == summary["applied"]
    assert (ws / store).read_text().count(marker) == live_text.count(marker) + done["applied"]


def test_a_sandbox_run_moves_no_cursor_and_writes_no_disposition(persona, cli, pinned_now):
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now)
    assert run_agent(ws).final["kind"] == "done"
    before = durable_rows(ws)
    assert before["cursors"] and before["dispositions"]
    with open(ws / "data" / "tickets.jsonl", "a") as fh:
        fh.write('{"key": "SUP-2100", "summary": "Sandbox-only ticket", "description": "d", '
                 '"requester": {"email": "pat@customer-one.example"}, "queue": "tier1", "priority": "P1", '
                 '"created": "2026-10-16T09:00:00Z"}\n')
    run_id = start(cli, ws, "shift", "2026-10-16T18:00:00+01:00", "--date", "2026-10-17", "--sandbox")["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    assert durable_rows(ws) == before
    d = state_root(ws, os.environ) / "runs" / run_id / "artifacts" / "seal"
    art = json.loads((d / ((d / "latest").read_text().strip() + ".json")).read_text())["data"]
    assert art["cursors_advanced"] == [] and "sandbox" in art["cursors_skipped"]


def test_a_sandbox_run_uses_a_run_nodes_dry_run_form_or_skips_it(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    count = tmp_path / "count.txt"
    add_nodes(ws, run_node("commit", "--count", str(count), "--label", "commit", after=["apply"],
                           dry_run=["--count", str(count), "--label", "commit-dry"])
              + run_node("reindex", "--count", str(count), "--label", "reindex", after=["apply"]))
    run_id = start(cli, ws, "shift", pinned_now, "--sandbox")["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    assert [ln.split()[0] for ln in count.read_text().splitlines()] == ["commit-dry"]
    d = state_root(ws, os.environ) / "runs" / run_id / "artifacts"
    out = {n: json.loads((d / n / ((d / n / "latest").read_text().strip() + ".json")).read_text())["data"]
           for n in ("commit", "reindex")}
    assert out["commit"]["outcome"] == "ok" and out["commit"]["dry_run"] is True
    assert out["commit"]["argv"][:2] == [sys.executable, str(STUB)]
    assert out["reindex"]["outcome"] == "skipped" and out["reindex"]["dry_run"] is True
    assert durable_rows(ws)["cursors"] == []
