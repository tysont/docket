"""``docket doctor`` (TD §15.4, §14.5; FR-ROB-06, NFR-05): writes nothing, checks every subprocess
unattended, and names the fix for each failure."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from docket.store.paths import state_root

STUB = Path(__file__).resolve().parents[2] / "fixtures" / "bin" / "calbridge_stub.py"


def checks(env) -> dict[str, dict]:
    rows = env["data"]["checks"] if env["ok"] else env["error"]["details"]["checks"]
    return {c["id"]: c for c in rows}


def tree(p: Path) -> dict[str, bytes]:
    return {str(f.relative_to(p)): f.read_bytes() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}


def test_doctor_writes_nothing_and_names_the_fix(persona, cli, isolate):
    ws = persona("support-lead")
    before_ws, before_state = tree(ws), tree(isolate["state"])
    code, env = cli("doctor", "--workspace", str(ws))
    assert code == 5 and env["error"]["code"] == "doctor.failed"
    c = checks(env)
    assert c["config"]["status"] == "ok" and c["state.root"]["status"] == "ok" and c["state.db"]["status"] == "ok"
    assert c["transport.pager-bridge"]["status"] == "ok"
    assert c["auth.kb"]["status"] == "fail" and "session expired" in c["auth.kb"]["detail"]
    assert c["auth.kb"]["fix"].startswith("Renew the knowledge base token")
    assert c["dest.worklog.location"]["status"] == "ok"
    assert all(r["fix"] for r in env["error"]["details"]["checks"] if r["status"] == "fail")
    assert tree(ws) == before_ws and tree(isolate["state"]) == before_state  # nothing written
    assert not state_root(ws, os.environ).exists()


def test_clean_workspace_passes(persona, cli):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("check: [docket-stub-auth, --fail]", "check: [docket-stub-auth, --ok]"))
    code, env = cli("doctor", "--workspace", str(ws))
    assert code == 0, env
    assert env["data"]["counts"]["fail"] == 0 and env["data"]["ok"] is True


def _auth(ws: Path, flag: str, timeout: float = 10) -> None:
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("check: [docket-stub-auth, --fail]",
                                           f"check: [docket-stub-auth, {flag}]\n      timeout_s: {timeout}"))


def test_auth_checks_run_unattended(persona, cli):
    ws = persona("support-lead")
    _auth(ws, "--env")  # passes only when DOCKET_NONINTERACTIVE=1
    code, env = cli("doctor", "--workspace", str(ws))
    assert checks(env)["auth.kb"]["status"] == "ok", checks(env)["auth.kb"]
    ws2 = persona("support-lead", under="ws2")
    _auth(ws2, "--stdin")  # passes only when stdin is closed
    code, env = cli("doctor", "--workspace", str(ws2))
    assert checks(env)["auth.kb"]["status"] == "ok", checks(env)["auth.kb"]


def test_a_hanging_auth_check_is_reported_as_interactive_or_hung(persona, cli):
    ws = persona("support-lead")
    _auth(ws, "--hang", timeout=0.5)
    code, env = cli("doctor", "--workspace", str(ws))
    c = checks(env)["auth.kb"]
    assert code == 5 and c["status"] == "fail" and c["detail"].startswith("interactive or hung")
    assert "never prompt" in c["fix"]


def test_exec_destination_permission_status_and_missing_programs(persona, cli, tmp_path):
    ws = persona("support-lead")
    _auth(ws, "--ok")
    state = tmp_path / "bridge.json"
    cfg = ws / "docket.yaml"
    dest = (f"  calendar:\n    adapter: calendar_exec\n    calendar: work\n"
            f"    program: [{sys.executable}, {STUB}, --state, {state}, --access, denied]\n")
    hooks = ("\nhooks:\n  notes-commit:\n    command: [no-such-program-xyz, commit]\n    events: [after_close]\n"
             "    dry_run: [docket-stub-auth, --ok]\n")
    cfg.write_text(cfg.read_text().replace("\ndestinations:\n", "\ndestinations:\n" + dest, 1) + hooks)
    code, env = cli("doctor", "--workspace", str(ws))
    c = checks(env)
    assert c["dest.calendar.program"]["status"] == "ok" and "status" in c["dest.calendar.program"]["detail"]
    assert c["dest.calendar.permission"]["status"] == "fail"
    assert c["dest.calendar.permission"]["detail"] == "platform permission: denied"
    assert "calendar access" in c["dest.calendar.permission"]["fix"]
    assert c["hook.notes-commit.command"]["status"] == "fail" and "no-such-program-xyz" in c["hook.notes-commit.command"]["fix"]
    assert c["hook.notes-commit.dry_run"]["status"] == "ok"
    assert not state.exists()  # the status verb wrote nothing
    # a bridge without the status verb is a warning, not a failure
    cfg.write_text(cfg.read_text().replace("--access, denied", "--caps, 'list,create'"))
    code, env = cli("doctor", "--workspace", str(ws))
    c = checks(env)
    assert c["dest.calendar.permission"]["status"] == "warn" and "status verb" in c["dest.calendar.permission"]["fix"]


def test_endpoints_are_not_contacted_without_probe(persona, cli):
    ws = persona("support-lead")
    _auth(ws, "--ok")
    cfg = ws / "docket.yaml"
    text = cfg.read_text().replace("metrics-api: { kind: replay, path: recordings/metrics-api.jsonl }",
                                   "metrics-api: { kind: http_json, url: 'https://metrics.invalid.example/api' }")
    text = text.replace("kb-bridge: { kind: replay, path: recordings/kb-bridge.jsonl }",
                        "kb-bridge: { kind: http_json, url: 'http://127.0.0.1:9/kb' }")
    cfg.write_text(text)
    code, env = cli("doctor", "--workspace", str(ws))  # the socket guard would fail any remote connect
    c = checks(env)
    assert c["transport.metrics-api"]["status"] == "ok" and "not contacted" in c["transport.metrics-api"]["detail"]
    assert c["transport.kb-bridge"]["status"] == "fail" and "127.0.0.1:9" in c["transport.kb-bridge"]["fix"]


def test_invalid_config_and_a_newer_database_fail(persona, cli):
    ws = persona("support-lead")
    root = state_root(ws, os.environ)
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "docket.db")
    db.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO schema_meta VALUES ('version', '999')")
    db.commit()
    db.close()
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text() + "\nunknown_section: 1\n")
    code, env = cli("doctor", "--workspace", str(ws))
    c = checks(env)
    assert code == 5 and c["config"]["status"] == "fail" and c["config"]["fix"]
    assert c["state.db"]["status"] == "fail" and "999" in c["state.db"]["fix"]
    assert c["transports"]["status"] == "skip"


# --- scoping to a workflow (AE-03: a failure the workflow never meets does not block it) ------------


HOOKS = ("\nhooks:\n  isolation-only:\n    command: [no-such-program-xyz]\n    events: [after_close]\n"
         "    workflows: [isolation]\n")


def test_doctor_workflow_checks_only_that_workflows_sources_destinations_and_hooks(persona, cli):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text() + HOOKS)
    code, env = cli("doctor", "--workflow", "shift", "--workspace", str(ws))
    assert code == 0, env  # kb (expired token) and the hook belong to isolation only
    c = checks(env)
    assert "auth.kb" not in c and "hook.isolation-only.command" not in c
    assert {"transport.pager-bridge", "dest.worklog.location", "config", "state.root"} <= set(c)
    assert not any(k.startswith(("transport.kb-bridge", "transport.metrics-api", "transport.history-bridge"))
                   for k in c)
    assert env["data"]["workflow"] == "shift" and all("shift" in r["workflows"] for r in env["data"]["checks"])
    code, env = cli("doctor", "--workflow", "isolation", "--workspace", str(ws))
    c = checks(env)
    assert code == 5 and c["auth.kb"]["status"] == "fail" and c["hook.isolation-only.command"]["status"] == "fail"


def test_doctor_without_a_workflow_reports_which_workflows_each_check_affects(persona, cli):
    ws = persona("support-lead")
    code, env = cli("doctor", "--workspace", str(ws))
    assert code == 5
    c = checks(env)
    assert c["auth.kb"]["workflows"] == ["isolation"]
    assert c["config"]["workflows"] == ["isolation", "shift"]
    assert c["transport.pager-bridge"]["workflows"] == ["isolation", "shift"]
    assert c["dest.worklog.location"]["workflows"] == ["isolation", "shift"]
    by = env["error"]["details"]["workflows"]
    assert by["shift"]["fail"] == 0 and by["isolation"]["fail"] >= 1
    assert "affects workflow(s) isolation" in env["error"]["message"]
    code, _ = cli("doctor", "--workspace", str(ws), json_out=False)
    assert "affects workflow(s) isolation" in cli.last_stderr and "docket doctor --workflow shift" in cli.last_stderr
    code, text = cli("doctor", "--workflow", "shift", "--workspace", str(ws), json_out=False)
    assert code == 0 and "workflow shift: ok" in text


def test_doctor_names_the_closest_workflow(persona, cli):
    ws = persona("support-lead")
    code, env = cli("doctor", "--workflow", "shfit", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_workflow"
    assert env["error"]["details"]["suggestion"] == "shift"
