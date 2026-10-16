"""Preset scope, ``docket init`` over every shipped preset, and preset descriptions (TD §3.3, AE-04)."""
from __future__ import annotations

import re

import pytest
from ruamel.yaml import YAML

from docket.config import check
from docket.config import presets as preset_mod
from docket.config.presets import preset_ids

pytestmark = pytest.mark.smoke

SOURCE_PRESETS = {"files/jsonl", "exec/jsonl-source", "http/json-api", "mcp/mail", "mcp/chat-codemode"}


def _yaml(path):
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def test_every_preset_declares_its_scope_and_list_and_show_report_it(cli):
    code, env = cli("presets", "list")
    assert code == 0
    rows = {r["id"]: r for r in env["data"]["presets"]}
    assert set(rows) == set(preset_ids())
    for pid, row in rows.items():
        assert row["scope"] == ("source" if pid in SOURCE_PRESETS else "workspace"), pid
        meta = _yaml(preset_mod.preset_path(pid)).get("preset") or {}
        assert meta.get("scope") == row["scope"], f"{pid} does not declare preset.scope"
    code, env = cli("presets", "show", "mcp/mail")
    assert code == 0 and env["data"]["scope"] == "source" and "preset" not in env["data"]["sections"]
    assert "sources.<id>.extends" in env["data"]["use"]
    code, env = cli("presets", "show", "options/basic")
    assert env["data"]["scope"] == "workspace" and env["data"]["sections"] == ["decisions"]


@pytest.mark.parametrize("pid", preset_ids())
def test_init_with_each_shipped_preset_writes_a_valid_workspace(tmp_path, cli, pid):
    target = tmp_path / "ws"
    code, env = cli("init", str(target), "--preset", pid)
    assert code == 0 and env["data"]["valid"] is True, env
    code, val = cli("config", "validate", "--workspace", str(target))
    assert code == 0, val
    doc = _yaml(target / "docket.yaml")
    if pid in SOURCE_PRESETS:
        assert pid not in doc["presets"]
        extended = [sid for sid, s in doc["sources"].items() if s.get("extends") == pid]
        assert len(extended) == 1
        sid = extended[0]
        # every placeholder is reported so the operator knows what to edit
        edits = {e["path"] for e in env["data"]["placeholders"]}
        assert any(p.startswith(f"sources.{sid}.") for p in edits), env["data"]
        assert all(e["preset"] == pid and "value" in e for e in env["data"]["placeholders"])
        assert f"sources.{sid}." in env["next"]["why"]
    else:
        assert pid in doc["presets"]


def test_init_with_the_reported_mix_lists_only_workspace_presets(tmp_path, cli):
    target = tmp_path / "ws"
    code, env = cli("init", str(target), "--preset", "files/jsonl", "--preset", "markdown/tasks-basic",
                    "--preset", "options/basic", "--preset", "policy/basic-defaults")
    assert code == 0 and env["data"]["valid"] is True, env
    doc = _yaml(target / "docket.yaml")
    assert doc["presets"] == ["markdown/tasks-basic", "options/basic", "policy/basic-defaults"]
    assert [s["extends"] for s in doc["sources"].values()] == ["files/jsonl"]
    assert env["data"]["presets"] == doc["presets"] and env["data"]["sources"] == list(doc["sources"])
    code, val = cli("config", "validate", "--workspace", str(target))
    assert code == 0, val


def test_init_with_every_preset_at_once_is_valid(tmp_path, cli):
    args = [a for pid in preset_ids() for a in ("--preset", pid)]
    code, env = cli("init", str(tmp_path / "ws"), *args)
    assert code == 0 and env["data"]["valid"] is True, env
    code, val = cli("config", "validate", "--workspace", str(tmp_path / "ws"))
    assert code == 0, val


def test_init_refuses_to_write_an_invalid_workspace(tmp_path, cli, monkeypatch):
    root = tmp_path / "presets"
    (root / "bad").mkdir(parents=True)
    (root / "bad" / "broken.yaml").write_text(
        "# A preset whose scaffold leaves a destination incomplete.\n"
        "preset: { scope: workspace }\n"
        "destinations:\n  cal:\n    adapter: calendar_exec\n", encoding="utf-8")
    monkeypatch.setattr(preset_mod, "PRESET_ROOT", root)
    target = tmp_path / "ws"
    code, env = cli("init", str(target), "--preset", "bad/broken")
    assert code == 3 and env["error"]["code"] == "config.invalid", env
    assert env["error"]["details"]["errors"] and not (target / "docket.yaml").exists()


def test_a_source_preset_in_top_level_presets_names_the_scope_and_the_fix(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docket.yaml").write_text(
        "version: 1\npresets: [files/jsonl]\nworkflows:\n  daily:\n    stages: [collect, assess, merge, decide, "
        "plan, approve, apply, close]\nprofile:\n  timezone: UTC\n  me: { name: Dana Reyes }\n"
        "destinations:\n  backlog: { adapter: markdown_tasks, path: tasks.md }\n", encoding="utf-8")
    _, issues, _ = check(ws)
    assert [i.code for i in issues] == ["config.preset_scope"], [i.as_dict() for i in issues]
    assert "sources.<id>.extends" in issues[0].message and issues[0].line == 2


def test_a_workspace_preset_in_extends_is_a_scope_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docket.yaml").write_text(
        "version: 1\nworkflows:\n  daily:\n    stages: [collect, assess, merge, decide, "
        "plan, approve, apply, close]\nprofile:\n  timezone: UTC\n  me: { name: Dana Reyes }\n"
        "sources:\n  feed: { extends: options/basic, path: x.jsonl, kind: files }\n"
        "destinations:\n  backlog: { adapter: markdown_tasks, path: tasks.md }\n", encoding="utf-8")
    _, issues, _ = check(ws)
    assert "config.preset_scope" in [i.code for i in issues]


TERMINAL = re.compile(r"[.!?]$")


def test_every_preset_description_is_a_complete_sentence(cli):
    code, env = cli("presets", "list")
    for row in env["data"]["presets"]:
        d = row["description"]
        assert d and d[0].isupper() and TERMINAL.search(d), (row["id"], d)
    agendas = next(r for r in env["data"]["presets"] if r["id"] == "markdown/agendas")
    assert agendas["description"].endswith("for the next meeting.")
