"""Rehearsal (TD §14.3, FR-ROB-03, IP M9 exit, TP §8.1).

A rehearsal copies a run into ``runs/<id>/rehearse-<n>/`` and drives the copy to its end
against sandbox twins and the run's recorded records. These tests hash everything a
rehearsal could touch (the live run directory, the database, the workspace files and the
destination files) before and after, and require them unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent


def digest_tree(p: Path, *, skip_rehearse: bool = False) -> dict[str, str]:
    out = {}
    for f in sorted(p.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(p)
        if skip_rehearse and any(part.startswith("rehearse-") for part in rel.parts):
            continue
        if f.name.endswith(("-wal", "-shm")):
            continue
        out[str(rel)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def snapshot(ws: Path, run_id: str) -> dict[str, dict[str, str]]:
    root = state_root(ws, os.environ)
    return {"workspace": digest_tree(ws), "state": digest_tree(root, skip_rehearse=True),
            "run": digest_tree(root / "runs" / run_id, skip_rehearse=True)}


def start(cli, ws: Path, workflow: str, now: str) -> str:
    code, env = cli("run", "start", workflow, "--now", now, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["run"]


@pytest.mark.parametrize("name,workflow,stop", [
    ("reference-like", "daily", lambda s: s["kind"] == "wait" and s["reason"] == "paused"),  # the evening pause
    ("support-lead", "shift", lambda s: s["kind"] == "ask"),  # no pause: rehearse at the first question
])
def test_both_personas_rehearse_to_close_writing_nothing_outside(persona, cli, pinned_now, name, workflow, stop):
    ws = persona(name)
    run_id = start(cli, ws, workflow, pinned_now)
    res = run_agent(ws, resume_paused=False, stop_at=stop)
    assert res.failures == []
    before = snapshot(ws, run_id)
    t0 = time.perf_counter()
    code, env = cli("run", "rehearse", "--workspace", str(ws))
    elapsed = time.perf_counter() - t0
    assert code == 0, env
    rep = env["data"]
    assert rep["ok"] is True, (rep["refusals"], rep["reached"])
    assert rep["reached"] == {"kind": "done"} and elapsed < 60
    assert snapshot(ws, run_id) == before  # nothing outside the rehearse directory changed
    stages = {s["stage"]: s for s in rep["stages"]}
    assert all(s["state"] == "current" for s in stages.values()), stages
    assert rep["operations"] and all(o["outcome"] in ("applied", "already_present") for o in rep["operations"])
    assert rep["refusals"] == [] and isinstance(rep["unexpected"], list)
    assert rep["answers"]["by_source"].get("first", 0) >= 1
    assert Path(rep["report"]).is_file() and Path(rep["report"]).parent.name == "rehearse-1"
    assert Path(rep["journal"]).is_file() and "/rehearse-1/" in rep["journal"]
    # the live run is still where it was
    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["run"] == run_id and env["data"]["step"]["kind"] == res.final["kind"]
    # a second rehearsal gets its own directory
    code, env = cli("run", "rehearse", "--run", run_id, "--answers", "recommended", "--workspace", str(ws))
    assert code == 0 and env["data"]["rehearsal"] == 2 and env["data"]["answers_mode"] == "recommended"


def test_rehearsal_synthesizes_tasks_and_reuses_accepted_results(persona, cli, pinned_now):
    ws = persona("reference-like")
    run_id = start(cli, ws, "daily", pinned_now)
    run_agent(ws, resume_paused=False, stop_at=lambda s: s["kind"] == "dispatch" and s["stage"] == "review")
    code, env = cli("run", "rehearse", "--workspace", str(ws))
    rep = env["data"]
    assert code == 0 and rep["ok"], rep["refusals"]
    assert {t["stage"] for t in rep["tasks"]} == {"review"}  # assess results were accepted live and reused
    assert all(t["synthetic"] and t["accepted"] for t in rep["tasks"])
    syn = json.loads((Path(rep["dir"]) / rep["tasks"][0]["result"]).read_text())
    assert all(row["verdict"] == "ask" and row["against"] for row in syn["items"])
    assert rep["resumed"] == ["decide"]


def test_recorded_and_file_answers_and_refusals_are_reported(persona, cli, pinned_now, tmp_path):
    ws = persona("support-lead")
    run_id = start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q1, q2 = res.final["questions"][0], res.final["questions"][1]
    code, env = cli("ask", "answer", q1["id"], "watch", "--workspace", str(ws))
    assert code == 0
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({q2["id"]: "close"}))
    code, env = cli("run", "rehearse", "--answers", f"file:{answers}", "--workspace", str(ws))
    rep = env["data"]
    assert code == 0 and rep["ok"], rep["refusals"]
    by_q = {a["question"]: a for a in rep["answers"]["rows"]}
    assert by_q[q2["id"]]["source"] == "file" and by_q[q2["id"]]["option"] == "close"
    assert q1["id"] not in by_q  # the recorded answer was kept, not re-asked
    assert rep["answers"]["by_source"]["recorded"] == 1
    # a refusal is reported with its code: a quality rule no answer can satisfy
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("      max_length: 90", "      max_length: 90\n      require: [due]"))
    code, env = cli("run", "rehearse", "--workspace", str(ws))
    rep = env["data"]
    assert code == 0 and rep["ok"] is False
    assert any(r["code"] == "dest.quality" for r in rep["refusals"]), rep["refusals"]
    code, env = cli("run", "rehearse", "--answers", "sometimes", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"


def test_rehearsal_replays_records_when_collect_reruns(persona, cli, pinned_now):
    """A configuration edit stales collect: the copy re-collects from recorded records, not from the sources."""
    ws = persona("support-lead")
    run_id = start(cli, ws, "shift", pinned_now)
    run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    (ws / "data" / "tickets.jsonl").write_text("")  # the live source changed; the rehearsal must not see it
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("    volatile: true\n    mapping:\n      id: \"$.key\"",
                                           "    volatile: true\n    description: edited\n    mapping:\n      id: \"$.key\"", 1))
    code, env = cli("run", "rehearse", "--workspace", str(ws))
    rep = env["data"]
    assert code == 0, env
    assert rep["replay"]["served"] > 0
    assert rep["operations"], "the recorded tickets still reach the plan"


def test_rehearse_at_pause_puts_the_command_in_the_pause_step(persona, cli, pinned_now):
    ws = persona("reference-like")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("    pause_before: decide\n", "    pause_before: decide\n    rehearse_at_pause: true\n", 1))
    start(cli, ws, "daily", pinned_now)
    res = run_agent(ws, resume_paused=False)
    step = res.final
    assert step["reason"] == "paused" and step["rehearse"]["argv"][1:3] == ["run", "rehearse"]
    assert "rehearse command" in step["instructions"]
    code, env = cli(*step["rehearse"]["argv"][1:])
    assert code == 0 and env["data"]["ok"], env["data"]["refusals"]


def test_no_run_to_rehearse(persona, cli):
    ws = persona("support-lead")
    code, env = cli("run", "rehearse", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "rehearse.no_run"
