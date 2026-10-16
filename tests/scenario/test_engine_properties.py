"""Engine properties: determinism (TP §5.2), staleness by digest (CFG-06), safe repetition (AE-06),
leases (TD §5.5) and a read-only ``next`` (TD §5.2)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent

CASES = [("reference-like", "daily"), ("support-lead", "shift")]


def tree(root: Path, *, skip: tuple[str, ...] = ("session",)) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.name not in skip and ".tmp" not in p.name}


def full_run(persona, cli, pinned_now, name, workflow, under, state, **kw):
    ws = persona(name, under=under)
    env = {"DOCKET_STATE_ROOT": str(state)}
    code, out = cli("run", "start", workflow, "--now", pinned_now, "--workspace", str(ws), env=env)
    assert code == 0, out
    res = run_agent(ws, env={**os.environ, **env}, **kw)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    return ws, state_root(ws, {**os.environ, **env}) / "runs" / out["data"]["run"]


def located(files: dict[str, bytes], *roots: Path) -> dict[str, bytes]:
    """Files with the given directories replaced by placeholders: prompts name their exact, absolute submit
    command (TD §9.3), the only place a run's own location appears in its files."""
    out = {}
    for k, v in files.items():
        for i, r in enumerate(roots):
            for form in sorted({str(r), str(r.resolve())}, key=len, reverse=True):
                v = v.replace(form.encode(), f"<root{i}>".encode())
        out[k] = v
    return out


@pytest.mark.parametrize("name,workflow", CASES)
def test_two_runs_in_separate_state_roots_are_byte_identical(persona, cli, pinned_now, tmp_path, name, workflow):
    ws_a, run_a = full_run(persona, cli, pinned_now, name, workflow, "a", tmp_path / "state-a")
    ws_b, run_b = full_run(persona, cli, pinned_now, name, workflow, "b", tmp_path / "state-b")
    ta = located(tree(run_a), run_a.parent.parent, ws_a)
    tb = located(tree(run_b), run_b.parent.parent, ws_b)
    assert ta.keys() == tb.keys()
    diff = [k for k in ta if ta[k] != tb[k]]
    assert diff == []
    assert any(k.startswith("artifacts/plan/") for k in ta) and "journal.md" in ta
    assert tree(ws_a) == tree(ws_b)


def test_reordered_records_give_the_same_artifacts(persona, cli, pinned_now, tmp_path):
    _, run_a = full_run(persona, cli, pinned_now, "support-lead", "shift", "a", tmp_path / "s-a")
    ws_b = persona("support-lead", under="b")
    data = ws_b / "data" / "tickets.jsonl"
    data.write_text("\n".join(reversed(data.read_text().splitlines())) + "\n")
    env = {"DOCKET_STATE_ROOT": str(tmp_path / "s-b")}
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws_b), env=env)
    res = run_agent(ws_b, env={**os.environ, **env})
    assert res.final["kind"] == "done"
    run_b = state_root(ws_b, {**os.environ, **env}) / "runs" / run_a.name
    assert tree(run_a / "artifacts") == tree(run_b / "artifacts")


def _status(cli, ws):
    code, env = cli("run", "status", "--workspace", str(ws))
    assert code == 0
    return {s["stage"]: s for s in env["data"]["stages"]}


def test_config_edit_mid_run_stales_exactly_the_declared_stages(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    assert res.failures == []
    st = _status(cli, ws)
    assert [st[s]["state"] for s in ("collect", "assess", "merge", "decide", "plan")] == ["current"] * 5
    assert st["approve"]["state"] == "waiting"
    assert not any(s["config_changed"] for s in st.values())

    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text() + "\nplanning:\n  policy: { granularity: 30m }\n")
    st = _status(cli, ws)
    assert {k for k, s in st.items() if s["config_changed"]} == {"plan"}
    assert [st[s]["state"] for s in ("collect", "assess", "merge", "decide")] == ["current"] * 4
    assert st["plan"]["state"] == "stale"
    assert st["approve"]["state"] == "blocked"

    cfg.write_text(cfg.read_text().replace("scoring: { floor: 40 }", "scoring: { floor: 40 }\n  budgets: { daily: 3 }"))
    st = _status(cli, ws)
    assert {k for k, s in st.items() if s["config_changed"]} == {"assess", "merge", "decide", "plan"}
    assert st["collect"]["state"] == "current"
    assert st["assess"]["state"] == "current"  # unchanged packs: accepted results carry forward
    assert st["merge"]["state"] == "stale"
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    kinds = [s["kind"] for s in res.steps]
    assert "dispatch" not in kinds  # no task was re-issued
    assert sum(1 for s in res.steps if s["kind"] == "ask" and s["stage"] == "decide") == 0  # answers kept


def test_stage_schema_bump_recomputes_exactly_that_stage(persona, cli, pinned_now, monkeypatch):
    from docket.engine.stages import STAGE_IMPLS
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    monkeypatch.setattr(STAGE_IMPLS["merge"], "schema_version", 2)
    st = _status(cli, ws)
    assert st["assess"]["state"] == "current" and st["merge"]["state"] == "stale"


@pytest.mark.parametrize("name,workflow", CASES)
def test_repeating_every_command_is_safe(persona, cli, pinned_now, tmp_path, name, workflow):
    ws_once, run_once = full_run(persona, cli, pinned_now, name, workflow, "once", tmp_path / "s1")
    ws_twice, run_twice = full_run(persona, cli, pinned_now, name, workflow, "twice", tmp_path / "s2",
                                   repeat_commands=True)
    assert tree(ws_once) == tree(ws_twice)
    a, b = tree(run_once), tree(run_twice)
    # receipts.jsonl and unexpected.jsonl may hold extra rows from repeats; every artifact is identical
    assert {k: v for k, v in a.items() if "artifacts" in k} == {k: v for k, v in b.items() if "artifacts" in k}


def test_commands_after_completion_are_no_ops(persona, cli, pinned_now):
    ws = persona("support-lead")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    res = run_agent(ws)
    assert res.final["kind"] == "done"
    root = state_root(ws, os.environ) / "runs" / run_id
    before_run, before_ws = tree(root), tree(ws)
    w = ["--workspace", str(ws), "--run", run_id]
    task_calls = [c for c in res.calls if c[:2] == ["task", "submit"]]
    answer_calls = [c for c in res.calls if c[:2] == ["ask", "answer"]]
    for argv in (["collect", *w], ["merge", *w], ["plan", "build", *w], ["apply", *w], ["close", *w],
                 ["task", "issue", *w], ["run", "resume", *w], task_calls[-1], answer_calls[0], answer_calls[-1],
                 ["run", "start", "shift", "--now", pinned_now, "--workspace", str(ws)]):
        code, env = cli(*argv)
        assert code == 0, (argv, env)
        assert env["data"].get("changed") in (False, None) and env["data"].get("created") in (False, None), argv
    assert tree(root) == before_run and tree(ws) == before_ws
    code, env = cli("ask", "answer", answer_calls[0][2], "close", *w)
    assert code == 7 and env["error"]["code"] == "run.sealed"


def test_lease_held_adopted_and_stolen(persona, cli, pinned_now):
    ws = persona("support-lead")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    other = {"DOCKET_SESSION": "agent-b"}
    code, env = cli("collect", "--workspace", str(ws), env=other)
    assert code == 6 and env["error"]["code"] == "lease.held"
    assert env["error"]["details"]["steal"][1:4] == ["run", "steal", run_id]
    code, env = cli("next", "--workspace", str(ws), env=other)
    assert env["data"]["step"]["kind"] == "wait" and env["data"]["step"]["reason"] == "lease_held"
    code, env = cli("collect", "--dry-run", "--workspace", str(ws), env=other)
    assert code == 0  # a dry run takes no lease
    code, env = cli("run", "steal", run_id, "--workspace", str(ws), env=other)
    assert code == 0 and env["data"]["owner"] == "agent-b"
    assert [w["code"] for w in env["warnings"]] == ["lease.stolen"]
    code, env = cli("collect", "--workspace", str(ws))  # the first session (token file) is now foreign
    assert code == 6
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    with db:
        db.execute("UPDATE leases SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (run_id,))
    db.close()
    code, env = cli("collect", "--workspace", str(ws))
    assert code == 0 and [w["code"] for w in env["warnings"]] == ["lease.adopted"]
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    with db:
        db.execute("UPDATE leases SET owner='agent-c', pid=999999 WHERE run_id=?", (run_id,))
    db.close()
    code, env = cli("next", "--workspace", str(ws))
    task = env["data"]["step"]["tasks"][0]  # dead owners do not block next either
    import json
    pack = json.loads(Path(task["pack_path"]).read_text())
    result = {"items": [{"id": it["id"], "source": it["source"], "keep": False, "score": 60}
                        for it in pack["items"]]}
    code, env = cli("task", "submit", task["task_id"], "-", "--workspace", str(ws), stdin=json.dumps(result))
    assert code == 0 and env["data"]["changed"] is True
    assert [w["code"] for w in env["warnings"]] == ["lease.adopted"]  # agent-c's pid is gone on this host


def _db_rows(path: Path) -> dict[str, list[tuple]]:
    db = sqlite3.connect(path)
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out = {n: sorted(db.execute(f"SELECT * FROM {n}").fetchall(), key=repr) for n in names}
    db.close()
    return out


def test_next_is_read_only_except_heartbeat(persona, cli, pinned_now):
    ws = persona("reference-like")
    code, env = cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    root = state_root(ws, os.environ)
    for stop in ("dispatch", "wait"):
        run_agent(ws, resume_paused=False, stop_at=lambda s, k=stop: s["kind"] == k)
        rows_before = _db_rows(root / "docket.db")
        files_before = tree(root, skip=("docket.db", "docket.db-wal", "docket.db-shm"))
        ws_before = tree(ws)
        for _ in range(3):
            code, env = cli("next", "--workspace", str(ws))
            assert code == 0
        rows_after = _db_rows(root / "docket.db")
        assert {k: v for k, v in rows_after.items() if k != "leases"} == \
               {k: v for k, v in rows_before.items() if k != "leases"}
        lb, la = rows_before["leases"], rows_after["leases"]
        assert [r[:5] for r in lb] == [r[:5] for r in la] and lb[0][0] == run_id  # only heartbeat_at moves
        assert tree(root, skip=("docket.db", "docket.db-wal", "docket.db-shm")) == files_before
        assert tree(ws) == ws_before
