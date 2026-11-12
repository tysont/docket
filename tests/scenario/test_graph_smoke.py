"""A small graph end to end, driven only by what ``docket next`` returns.

The workspace reads one files source (``gather``), has an agent assess it, merges, asks
the operator, plans, asks for approval and applies to a Markdown task list. A second,
independent ``run`` node runs beside the read. Every ready step is done, run steps by
their argv, dispatch steps by writing a valid assess result and running its submit argv,
ask steps by answering, until the run is sealed.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from docket.cli import main

NOW = "2026-10-15T18:00:00+00:00"

ITEMS = [
    {"id": "m1", "title": "Send the quarterly report", "text": "Please send the quarterly report by Friday.",
     "from": "pat@example.com", "at": "2026-10-15T09:00:00Z"},
    {"id": "m2", "title": "Review the launch plan", "text": "Can you review the launch plan this week?",
     "from": "lin@example.com", "at": "2026-10-15T10:00:00Z"},
]

GRAPH = """\
graphs:
  daily:
    gather: { read: inbox }
    assess: { judge: assess, from: [gather] }
    merge: { merge: [assess] }
    decide: { ask: default, from: [merge] }
    plan: { plan: [decide] }
    approve: { approve: plan }
    apply: { apply: approve }
    touch: { run: ["true"], description: "A command that needs nothing and hands on nothing." }
"""

BASE = """\
version: 1
profile:
  timezone: UTC
  me: { name: Sam Doe, handles: { mail: sam@example.com } }
sources:
  inbox:
    kind: files
    path: data/inbox.jsonl
    mapping: { id: "$.id", title: "$.title", text: "$.text", from: "$.from", at: "$.at" }
destinations:
  backlog: { adapter: markdown_tasks, path: tasks.md }
"""


def _workspace(root: Path, graph: str = GRAPH) -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "inbox.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ITEMS), encoding="utf-8")
    (root / "docket.yaml").write_text(BASE + graph, encoding="utf-8")
    return root


def _call(ws: Path, argv: list[str]) -> tuple[int, dict[str, Any]]:
    if argv and argv[0] == "docket":
        argv = argv[1:]
    if "--json" not in argv:
        argv = [*argv, "--json"]
    if "--workspace" not in argv:
        argv = [*argv, "--workspace", str(ws)]
    out = io.StringIO()
    code = main(argv, stdout=out, stderr=io.StringIO(), cwd=str(ws))
    return code, json.loads(out.getvalue())


def _ok(ws: Path, argv: list[str]) -> dict[str, Any]:
    code, env = _call(ws, argv)
    assert code == 0 and env["ok"], (argv, env.get("error"))
    return env


def _assess_result(pack: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for it in pack["items"]:
        line = it["lines"][0]
        rows.append({"id": it["id"], "source": it["source"], "keep": True, "score": 70, "kind": "request",
                     "owner": "me", "ask": f"Follow up on {it['title'][:60]}",
                     "evidence": [{"line": line["line"], "quote": line["text"][:200]}]})
    return {"items": rows}


def _fill(argv: list[str], question: str, option: str) -> list[str]:
    return [question if a == "{question}" else option if a == "{option}" else a for a in argv]


def _do(ws: Path, step: dict[str, Any]) -> None:
    kind = step["kind"]
    if kind == "run":
        _ok(ws, step["command"]["argv"])
    elif kind == "dispatch":
        for t in step["tasks"]:
            pack = json.loads(Path(t["pack_path"]).read_text(encoding="utf-8"))
            assert t["kind"] == "assess"
            Path(t["result_path"]).write_text(json.dumps(_assess_result(pack)), encoding="utf-8")
            _ok(ws, t["submit"]["argv"])
    elif kind == "ask":
        for q in step["questions"]:
            ids = [o["id"] for o in q["options"]]
            option = "yes" if q["kind"] == "approve" else ("task" if "task" in ids else ids[0])
            _ok(ws, _fill(step["answer"]["argv"], q["id"], option))
    else:
        raise AssertionError(f"unexpected step: {step}")


def test_graph_runs_end_to_end(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws")
    _ok(ws, ["config", "validate"])

    shown = _ok(ws, ["graph", "show", "daily"])["data"]
    assert [r["node"] for r in shown["nodes"]] == ["gather", "assess", "merge", "decide", "plan", "approve",
                                                   "apply", "touch"]
    by_node = {r["node"]: r for r in shown["nodes"]}
    assert by_node["assess"]["by"] == "agent" and by_node["decide"]["by"] == "operator"
    assert by_node["assess"]["from"] == ["gather"]

    started = _ok(ws, ["run", "start", "daily", "--now", NOW])["data"]
    assert started["created"] is True
    run_id = started["run"]

    first = _ok(ws, ["next"])["data"]
    assert first["done"] is False
    ready = {(s["kind"], s["node"]) for s in first["steps"]}
    assert ready == {("run", "gather"), ("run", "touch")}

    seen: list[tuple[str, str]] = []
    for _ in range(40):
        data = _ok(ws, ["next"])["data"]
        if data["done"]:
            break
        assert data["steps"], data["nodes"]
        for step in data["steps"]:
            assert step["kind"] != "wait", step
            seen.append((step["kind"], step["node"]))
            _do(ws, step)
    else:
        raise AssertionError(f"the run did not finish; steps seen: {seen}")

    assert ("dispatch", "assess") in seen
    assert ("ask", "decide") in seen and ("ask", "approve") in seen
    assert ("run", "seal") in seen
    assert data["summary"]["status"] == "sealed"

    tasks = (ws / "tasks.md").read_text(encoding="utf-8")
    assert "- [ ] " in tasks
    assert "quarterly report" in tasks and "launch plan" in tasks

    status = _ok(ws, ["run", "status", run_id])["data"]
    assert status["header"]["status"] == "sealed"
    assert status["header"]["graph"] == "daily"
    assert all(n["state"] == "current" for n in status["nodes"])
    assert [n["node"] for n in status["nodes"]][-1] == "seal"


def test_config_validate_refuses_bad_graphs(tmp_path: Path) -> None:
    cyclic = """\
graphs:
  daily:
    gather: { read: inbox }
    first: { run: ["true"], after: [second] }
    second: { run: ["true"], after: [first] }
"""
    ws = _workspace(tmp_path / "cyclic", cyclic)
    code, env = _call(ws, ["config", "validate"])
    assert code != 0 and env["error"]["code"] == "config.invalid"
    assert "cycle" in json.dumps(env["error"])

    unknown = """\
graphs:
  daily:
    gather: { read: inbox }
    decide: { ask: default, from: [missing] }
"""
    ws = _workspace(tmp_path / "unknown", unknown)
    code, env = _call(ws, ["config", "validate"])
    assert code != 0 and env["error"]["code"] == "config.invalid"
    assert "'missing', which is not a node of the graph" in json.dumps(env["error"])


def test_a_step_says_how_long_its_command_may_take(tmp_path: Path) -> None:
    """The driver bounds each command by the step's ``timeout_s``. A read is bounded by its
    source's own time budget, or generously when it has none; a run node by its own timeout."""
    graph = GRAPH.replace('run: ["true"],', 'run: ["true"], timeout_s: 300,')
    ws = _workspace(tmp_path, graph)
    steps = {s["node"]: s for s in _ok(ws, ["run", "start", "daily", "--now", NOW])["data"]["steps"]}
    assert steps["gather"]["timeout_s"] == 1800 and steps["touch"]["timeout_s"] == 330
    (ws / "docket.yaml").write_text((BASE + graph).replace(
        "    kind: files\n", "    kind: files\n    budget: { max_seconds: 240 }\n"), encoding="utf-8")
    steps = {s["node"]: s for s in _ok(ws, ["run", "start", "daily", "--now", NOW, "--new"])["data"]["steps"]}
    assert steps["gather"]["timeout_s"] == 300


def test_a_sealed_run_reads_as_it_was_sealed_whatever_the_configuration_says_now(tmp_path: Path) -> None:
    """After the seal, a change to the configuration must not make any node of that run look
    stale or ready: the run is history."""
    ws = _workspace(tmp_path)
    run = _ok(ws, ["run", "start", "daily", "--now", NOW])["data"]["run"]
    for _ in range(30):
        data = _ok(ws, ["next", "--run", run, "--full"])["data"]
        if data["done"]:
            break
        for step in data["steps"]:
            _do(ws, step)
    assert data["done"]
    (ws / "docket.yaml").write_text((ws / "docket.yaml").read_text().replace(
        "path: tasks.md }", "path: tasks.md, line_template: '- [ ] {title} (changed)' }"), encoding="utf-8")
    nodes = _ok(ws, ["run", "status", run])["data"]["nodes"]
    assert nodes and {n["state"] for n in nodes} == {"current"}
    assert _ok(ws, ["next", "--run", run])["data"]["done"] is True
