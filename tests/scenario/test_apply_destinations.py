"""Executor scenarios (TD §13.3, IP M6 exit): refusals beside successes, crash after each operation,
external failure and retry, a shared calendar through the exec protocol, and ``docket plugin check``."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from docket.apply import executor
from docket.destinations.markdown_io import line_digest
from docket.store.paths import state_root
from tests.agent_loop import run_agent

STUB = Path(__file__).resolve().parents[1] / "fixtures" / "bin" / "calbridge_stub.py"


def run_context(ws: Path):
    from docket.config import load_workspace
    from docket.engine.context import RunContext
    from docket.store.runs import Store, resolve_run
    store = Store(state_root(ws, os.environ))
    return RunContext(load_workspace(ws), resolve_run(store, None, start_hint="-"), os.environ)


def to_apply(persona, cli, pinned_now, name="support-lead", workflow="shift"):
    ws = persona(name)
    cli("run", "start", workflow, "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "apply")
    assert res.failures == [] and res.final["stage"] == "apply"
    return ws, res.final


def test_line_changed_is_refused_while_other_operations_apply(persona, cli, pinned_now):
    ws, _ = to_apply(persona, cli, pinned_now)
    todo = ws / "worklog" / "todo.md"
    todo.parent.mkdir(parents=True, exist_ok=True)
    todo.write_text("## Today\nTODO Old ticket | queue=tier1 | prio=P3\n")
    rc = run_context(ws)
    from docket.destinations import for_run
    from docket.destinations.base import Action
    d = for_run(rc, "worklog")
    [close] = d.preview(Action(kind="task.close", destination="worklog",
                               target="TODO Old ticket | queue=tier1 | prio=P3"), d.snapshot())
    [add_a] = d.preview(Action(kind="task.add", destination="worklog", fields={"title": "First new"}), d.snapshot())
    [add_b] = d.preview(Action(kind="task.add", destination="worklog", fields={"title": "Second new"}), d.snapshot())
    from docket.destinations import snapshot_digests
    before = snapshot_digests(rc, ["worklog"])
    # the operator edits the line after the plan was built
    todo.write_text(todo.read_text().replace("prio=P3", "prio=P1"))
    assert snapshot_digests(rc, ["worklog"]) != before  # what M8 freshness compares
    rows = executor.execute(rc, [add_a.dump(), close.dump(), add_b.dump()])
    assert [r["outcome"] for r in rows] == ["applied", "refused", "applied"]
    err = rows[1]["error"]
    assert err["code"] == "dest.line_changed" and "prio=P1" in err["details"]["current"]
    text = todo.read_text()
    assert "TODO Old ticket | queue=tier1 | prio=P1" in text and "DONE" not in text  # not overwritten
    assert "TODO First new" in text and "TODO Second new" in text
    assert close.old_digest == line_digest("TODO Old ticket | queue=tier1 | prio=P3")


def test_dependants_of_a_refused_operation_are_blocked(persona, cli, pinned_now):
    ws, _ = to_apply(persona, cli, pinned_now)
    rc = run_context(ws)
    from docket.destinations.base import make_op
    bad = make_op("worklog", "replace", ident="## Today", path="worklog/todo.md", anchor="## Today",
                  old_digest="0" * 64, old_line="x", line="y")
    dep = make_op("worklog", "append", ident="## Today", path="worklog/todo.md", anchor="## Today",
                  line="TODO depends on the replace", depends_on=[bad.op_id])
    rows = executor.execute(rc, [bad.dump(), dep.dump()])
    assert [r["outcome"] for r in rows] == ["refused", "blocked"] and rows[1]["blocked_by"] == [bad.op_id]


@pytest.mark.parametrize("hook", ["AFTER_WRITE", "AFTER_RECEIPT"])
def test_crash_after_each_operation_resumes_without_duplicates(persona, cli, pinned_now, monkeypatch, hook):
    ws, step = to_apply(persona, cli, pinned_now)
    crashed: set[str] = set()

    def crash(op):
        if op.op_id not in crashed:
            crashed.add(op.op_id)
            raise RuntimeError(f"injected crash after {op.op_id}")

    monkeypatch.setattr(executor, hook, crash)
    argv = step["command"]["argv"][1:]
    codes = []
    for _ in range(40):
        code, env = cli(*argv)
        codes.append(code)
        if code == 0:
            break
        assert env["error"]["code"] == "internal.bug"
    n_ops = len(crashed)
    assert n_ops == 12 and codes.count(70) == n_ops and codes[-1] == 0
    monkeypatch.setattr(executor, hook, None)
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    lines = [ln for ln in (ws / "worklog" / "todo.md").read_text().splitlines() if ln.startswith("TODO")]
    assert len(lines) == len(set(lines)) == n_ops
    assert res.final["summary"]["operations"] == n_ops
    import sqlite3
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    rows = db.execute("SELECT outcome, verify_status FROM receipts").fetchall()
    db.close()
    assert len(rows) == n_ops and {v for _, v in rows} == {"verified"}
    # after the receipt commit the repeat keeps "applied"; before it, the repeat finds the line in place
    assert {o for o, _ in rows} == ({"applied"} if hook == "AFTER_RECEIPT" else {"already_present"})


def test_external_failure_waits_with_apply_partial_and_retries(persona, cli, pinned_now):
    ws, step = to_apply(persona, cli, pinned_now)
    blocker = ws / "worklog" / "todo.md"
    blocker.mkdir(parents=True)  # writes fail until the operator fixes it
    code, env = cli(*step["command"]["argv"][1:])
    assert code == 5 and env["error"]["code"] == "dest.apply_failed"
    assert env["next"]["command"][1] == "apply"
    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["step"]["kind"] == "wait" and env["data"]["step"]["reason"] == "apply_partial"
    blocker.rmdir()
    code, env = cli(*step["command"]["argv"][1:])  # the retry command from the wait step
    assert code == 0, env
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    assert res.final["summary"]["applied"] == 12


SHARED_CAL = """
  calendar:
    adapter: calendar_exec
    visibility: shared
    calendar: work
    program: [{py}, {stub}, --state, {state}]
    redact: {{ people: true }}
"""


def with_exec_calendar(ws: Path, tmp_path: Path) -> Path:
    cfg = next(p for p in sorted(ws.glob("*.yaml")) if "\ndestinations:\n" in "\n" + p.read_text())
    state = tmp_path / "bridge-state.json"
    text = ("\n" + cfg.read_text()).replace("\ndestinations:\n", "\ndestinations:\n" + SHARED_CAL.format(
        py=sys.executable, stub=STUB, state=state).lstrip("\n"), 1)
    cfg.write_text(text[1:])
    return state


def test_shared_exec_calendar_refuses_a_protected_entity_and_writes_owned_blocks(persona, cli, pinned_now, tmp_path):
    ws, _ = to_apply(persona, cli, pinned_now, "reference-like", "triage")
    state = with_exec_calendar(ws, tmp_path)
    rc = run_context(ws)
    from docket.destinations import for_run
    from docket.destinations.base import Action
    d = for_run(rc, "calendar")
    try:
        slot = {"start": "2026-10-19T09:00:00+01:00", "end": "2026-10-19T10:00:00+01:00", "note": ""}
        with pytest.raises(Exception) as e:
            d.preview(Action(kind="calendar.place", destination="calendar", key="q1",
                             fields={"title": "AcmeCorp export latency follow-up", **slot}), d.snapshot())
        assert getattr(e.value, "code", None) == "dest.redaction_leak"
        [op] = d.preview(Action(kind="calendar.place", destination="calendar", key="q2",
                                fields={"title": "Acme Corp latency: call Kim Osei", **slot}), d.snapshot())
        assert op.title == "[redacted] latency: call [person]"
    finally:
        d.close()
    rows = executor.execute(rc, [op.dump()])
    assert rows[0]["outcome"] == "applied" and rows[0]["verify_status"] == "verified"
    assert "[docket:" + op.key + "]" in state.read_text()
    assert executor.execute(rc, [op.dump()])[0]["outcome"] == "applied"  # verified receipt: skipped


def test_plugin_list_and_check(persona, cli, tmp_path):
    ws = persona("support-lead")
    state = with_exec_calendar(ws, tmp_path)
    code, env = cli("plugin", "list", "--workspace", str(ws))
    assert code == 0
    assert {a["name"] for a in env["data"]["adapters"]} >= {"markdown_tasks", "calendar_exec", "message_mcp"}
    assert {d["id"] for d in env["data"]["destinations"]} == {"worklog", "calendar"}
    for name in ("worklog", "calendar", "journal"):
        code, env = cli("plugin", "check", name, "--workspace", str(ws))
        assert code == 0, env
        assert env["data"]["report"]["ok"]
    assert not state.exists()  # the exec check ran on the program's scratch target only
    code, env = cli("plugin", "check", "nonesuch", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "plugin.unknown"
    code, env = cli("plugin", "check", "calendar_exec", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "plugin.unknown"


def test_quality_refusal_names_the_answer_override(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("      max_length: 90", "      max_length: 90\n      require: [due]"))
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "take", "--workspace", str(ws))
    assert code == 4 and env["error"]["code"] == "dest.quality" and env["error"]["kind"] == "integrity"
    fix = env["error"]["details"]["fix"]
    assert fix == env["error"]["hint"] and f"ask answer {q['id']} take --due <date>" in fix
    code, env = cli("ask", "answer", q["id"], "take", "--due", "monday", "--workspace", str(ws))
    assert code == 0 and env["data"]["preview"]["line"].endswith("| due=2026-10-19")
