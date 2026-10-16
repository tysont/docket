"""Envelope and exit-code contract (TD §15.2, §15.3, AE-02, AE-03)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from docket.cli import catalog
from docket.cli.errors import DocketError
from docket.cli.registry import REGISTRY
from tests.agent_loop import run_agent

pytestmark = pytest.mark.smoke

OK_KEYS = {"ok", "command", "workspace", "run", "data", "warnings", "unexpected", "next"}
ERR_KEYS = {"ok", "command", "workspace", "run", "error", "warnings", "unexpected", "next"}


def assert_error(code, env, expected_code, kind):
    assert set(env) == ERR_KEYS
    assert env["ok"] is False
    e = env["error"]
    assert e["code"] == expected_code and e["kind"] == kind
    assert set(e) == {"code", "kind", "message", "hint", "retryable", "details"}
    assert e["message"]
    assert code == catalog.KINDS[kind]


def test_ok_envelope_shape(cli):
    code, env = cli("version")
    assert code == 0 and set(env) == OK_KEYS and env["ok"] is True and env["command"] == "version"


def test_input_error(cli):
    code, env = cli("schema", "nope")
    assert_error(code, env, "input.unknown_schema", "input")
    code, env = cli("frobnicate")
    assert_error(code, env, "input.unknown_command", "input")
    code, env = cli("run", "start")
    assert_error(code, env, "input.bad_argument", "input")
    code, env = cli("run", "bogus")
    assert_error(code, env, "input.unknown_command", "input")
    assert "run start" in env["error"]["details"]["subcommands"]


def test_config_error(cli, tmp_path):
    code, env = cli("config", "validate", cwd=tmp_path)
    assert_error(code, env, "workspace.not_found", "config")


def test_state_error(cli, persona):
    ws = persona("support-lead")
    code, env = cli("run", "status", "--workspace", str(ws))
    assert_error(code, env, "run.none_open", "state")
    assert env["error"]["hint"] == "docket run start shift"


def test_integrity_error(cli, persona, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    cli("collect", "--workspace", str(ws))
    code, env = cli("next", "--workspace", str(ws))
    task = env["data"]["step"]["tasks"][0]
    pack = json.loads(Path(task["pack_path"]).read_text())
    rows = [{"id": it["id"], "keep": True, "score": 50} for it in pack["items"]][1:]
    rows.append({"id": "not-in-pack", "keep": False, "score": 1})
    code, env = cli("task", "submit", task["task_id"], "-", "--workspace", str(ws), stdin=json.dumps({"items": rows}))
    assert_error(code, env, "integrity.partition", "integrity")
    assert env["error"]["details"]["extra"] == ["not-in-pack"] and len(env["error"]["details"]["missing"]) == 1
    code, env = cli("task", "submit", task["task_id"], "-", "--workspace", str(ws), stdin="{not json")
    assert_error(code, env, "input.bad_json", "input")
    code, env = cli("task", "submit", task["task_id"], "-", "--workspace", str(ws),
                    stdin=json.dumps({"items": [{"id": "x", "keep": "maybe", "score": 500}]}))
    assert_error(code, env, "task.invalid_result", "input")


def test_external_error(cli, persona, pinned_now):
    ws = persona("support-lead")
    (ws / "worklog" / "todo.md").mkdir(parents=True)  # the task store path is a directory: writes fail
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "apply")
    assert res.failures == []
    code, env = cli(*res.final["command"]["argv"][1:])
    assert_error(code, env, "dest.apply_failed", "external")
    assert env["error"]["retryable"] is True


def test_lease_error(cli, persona, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("collect", "--workspace", str(ws), env={"DOCKET_SESSION": "other-agent"})
    assert_error(code, env, "lease.held", "lease")
    assert env["next"]["command"][:3] == ["docket", "run", "steal"]


def test_internal_error_writes_traceback_file(cli, monkeypatch, isolate):
    def boom(ctx, ns):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(REGISTRY["version"], "handler", boom)
    code, env = cli("version")
    assert_error(code, env, "internal.bug", "internal")
    path = Path(env["error"]["details"]["traceback_path"])
    assert path.is_file() and "synthetic failure" in path.read_text()
    assert str(path).startswith(str(isolate["state"]))
    assert "Traceback" not in cli.last_stdout and "synthetic failure" not in cli.last_stdout
    code, out = cli("version", json_out=False)
    assert code == 70 and "Traceback" not in out and "Traceback" not in cli.last_stderr


def test_human_output_has_summary_and_next(cli, persona, pinned_now):
    ws = persona("support-lead")
    code, out = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws), json_out=False)
    assert code == 0
    lines = out.strip().splitlines()
    assert lines[0].startswith("started run 2026-10-16-shift")  # Lisbon is past midnight
    assert lines[-1].startswith("next: docket collect")


def test_every_kind_has_an_exit_code_and_catalog_is_consistent(cli):
    for code_name, (kind, retry, desc) in catalog.ERRORS.items():
        assert kind in catalog.KINDS and desc and "." in code_name
    assert catalog.KINDS == {"input": 2, "config": 3, "integrity": 4, "external": 5, "lease": 6,
                             "state": 7, "internal": 70}
    code, env = cli("help", "errors")
    assert code == 0 and {r["code"] for r in env["data"]["errors"]} == set(catalog.ERRORS)
    with pytest.raises(ValueError):
        DocketError("not.in.catalog")
    with pytest.raises(ValueError):
        DocketError("lease.held", "input")


def test_every_error_code_raised_in_src_is_cataloged():
    import re
    src = Path(__file__).resolve().parents[2] / "src" / "docket"
    pat = re.compile(r"(?:Error|DocketError)\(\s*\"([a-z_]+\.[a-z_]+)\"")
    used = set()
    for f in src.rglob("*.py"):
        used |= set(pat.findall(f.read_text()))
    assert used and used <= set(catalog.ERRORS), used - set(catalog.ERRORS)


def test_help_json_describes_every_command(cli):
    code, env = cli("help")
    names = {c["name"] for c in env["data"]["commands"]}
    assert names == set(REGISTRY)
    for c in env["data"]["commands"]:
        args = {a["name"] for a in c["arguments"]}
        assert "--json" in args and c["effects"] and c["summary"]
        if c["mutating"]:
            assert "--dry-run" in args, c["name"]
    code, env = cli("help", "run", "start")
    d = env["data"]["command"]
    assert d["name"] == "run start" and d["examples"] and any(a["name"] == "--sandbox" for a in d["arguments"])
    code, env = cli("run", "start", "--help")
    assert code == 0 and env["data"]["command"]["name"] == "run start"


def test_schema_list_prints_every_model(cli):
    code, env = cli("schema", "list")
    assert code == 0
    names = env["data"]["schemas"]
    for required in ("envelope", "error", "step", "config", "profile", "workflow", "assess-result",
                     "review-result", "plan", "answer", "run-header", "record"):
        assert required in names
    for n in names:
        code, env = cli("schema", n)
        assert code == 0 and isinstance(env["data"]["schema"], dict), n


def test_no_command_reads_process_environment_home(cli, persona, pinned_now, isolate, monkeypatch):
    """State goes under the env given to main (fake HOME / DOCKET_STATE_ROOT), never elsewhere."""
    ws = persona("support-lead")
    monkeypatch.delenv("DOCKET_STATE_ROOT")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0
    base = Path(os.environ["HOME"]) / ".local" / "state" / "docket"
    assert any(base.iterdir())
