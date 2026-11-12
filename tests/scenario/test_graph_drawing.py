"""``docket graph show`` and ``docket run status --graph`` through the CLI (TD §5.6): the
drawing, the flat list, Mermaid, and a run's state drawn on the graph."""
from __future__ import annotations

import json
from pathlib import Path

from tests.agent_loop import run_agent

NOW = "2026-10-15T18:00:00+00:00"

WORKSPACE = """\
version: 1
profile:
  timezone: UTC
  me: { name: Sam Doe, handles: { mail: sam@example.org } }
sources:
  inbox:
    kind: files
    path: data/inbox.jsonl
    mapping: { id: "$.id", title: "$.title", text: "$.text", from: "$.from", at: "$.at" }
destinations:
  backlog: { adapter: markdown_tasks, path: tasks.md }
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
  other:
    gather: { read: inbox }
"""

ITEMS = [
    {"id": "m1", "title": "Send the quarterly report", "text": "Please send the quarterly report by Friday.",
     "from": "pat@example.org", "at": "2026-10-15T09:00:00Z"},
]

DRAWING = """\
graph daily: 8 node(s) in 7 column(s), then the seal
[verb] docket computes  {verb} an agent judges  (verb) the operator is asked
──▶ hands on data  ┄┄▷ only orders  +N edges listed below  » goes on below

      0             1             2             3
      gather ─────▶ assess ─────▶ merge ──────▶ decide ───»
      [read]        {judge}       [merge]       (ask)
      touch
      [run]

      4             5             6             7
  »─▶ plan ───────▶ approve ────▶ apply ┄┄┄┄┄┄▷ seal
      [plan]        (approve)     [apply]       [seal] +1

edges not drawn in the rows:
  seal ◀┄┄ touch

descriptions:
  touch: A command that needs nothing and hands on nothing.
"""

STARTED = """\
run 2026-10-15-daily of graph daily (open)
[verb] docket computes  {verb} an agent judges  (verb) the operator is asked
──▶ hands on data  ┄┄▷ only orders  +N edges listed below  » goes on below
✓ done  ● ready now  ○ waiting  ✗ blocked

      0             1             2             3
      ● gather ───▶ ○ assess ───▶ ○ merge ────▶ ○ decide ─»
      [read]        {judge}       [merge]       (ask)
      ● touch
      [run]

      4             5             6             7
  »─▶ ○ plan ─────▶ ○ approve ──▶ ○ apply ┄┄┄┄▷ ○ seal
      [plan]        (approve)     [apply]       [seal] +1

edges not drawn in the rows:
  seal ◀┄┄ touch

ready now: gather (docket), touch (docket)
waiting:   assess (on gather), merge (on assess), decide (on merge),
           plan (on decide), approve (on plan), apply (on approve),
           seal (on apply, touch)

descriptions:
  touch: A command that needs nothing and hands on nothing.
"""


def _workspace(root: Path) -> Path:
    (root / "data").mkdir(parents=True)
    (root / "data" / "inbox.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ITEMS), encoding="utf-8")
    (root / "docket.yaml").write_text(WORKSPACE, encoding="utf-8")
    return root


def _text(cli, ws: Path, *args: str) -> str:
    code, out = cli(*args, "--workspace", str(ws), json_out=False)
    assert code == 0, cli.last_stderr
    return out


def test_graph_show_draws_the_graph_lists_it_and_writes_mermaid(cli, tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws")
    assert _text(cli, ws, "graph", "show", "daily") == DRAWING
    assert _text(cli, ws, "graph", "show", "daily", "--ascii").isascii()
    flat = _text(cli, ws, "graph", "show", "daily", "--list").splitlines()
    assert flat[:2] == ["gather (read, docket)", "assess (judge, agent) <- gather"]
    assert flat[-1] == "touch (run, docket)"
    mer = _text(cli, ws, "graph", "show", "daily", "--format", "mermaid")
    assert mer.startswith("flowchart LR\n") and "  n_apply -.-> seal\n" in mer and "  n_gather --> n_assess\n" in mer

    code, env = cli("graph", "show", "daily", "--workspace", str(ws))
    assert code == 0
    data = env["data"]
    assert data["graph"] == "daily" and "run" not in data
    by = {r["node"]: r for r in data["nodes"]}
    assert (by["gather"]["column"], by["gather"]["row"]) == (0, 0)
    assert (by["touch"]["column"], by["touch"]["row"]) == (0, 1)
    assert (by["apply"]["column"], by["apply"]["row"]) == (6, 0)
    assert by["assess"]["by"] == "agent" and by["assess"]["from"] == ["gather"] and by["assess"]["after"] == []
    assert data["seal"] == {"column": 7, "row": 0, "after": ["apply", "touch"]}


def test_a_run_is_drawn_on_its_graph_as_it_moves(cli, tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws")
    code, env = cli("run", "start", "daily", "--now", NOW, "--workspace", str(ws))
    assert code == 0
    run_id = env["data"]["run"]
    assert _text(cli, ws, "graph", "show", "daily", "--run") == STARTED
    status = _text(cli, ws, "run", "status", "--graph")
    assert status.startswith(STARTED) and "next: docket do gather" in status

    res = run_agent(ws, cwd=ws, stop_at=lambda s: s["kind"] == "ask" and s["node"] == "decide")
    assert res.final["kind"] == "ask", res.final
    code, env = cli("graph", "show", "--run", run_id, "--workspace", str(ws))
    assert code == 0 and env["run"] == run_id and env["data"]["status"] == "open"
    marks = {r["node"]: r["mark"] for r in env["data"]["nodes"]}
    assert marks == {"gather": "done", "touch": "done", "assess": "done", "merge": "done", "decide": "ready",
                     "plan": "waiting", "approve": "waiting", "apply": "waiting"}
    assert env["data"]["seal"]["mark"] == "waiting" and env["data"]["seal"]["waiting_on"] == ["apply"]
    text = _text(cli, ws, "run", "status", run_id, "--graph", "--ascii")
    assert "+ merge ----> ! decide" in text and "ready now: decide (operator)" in text

    code, env = cli("run", "status", run_id, "--graph", "--workspace", str(ws))
    rows = {n["node"]: n for n in env["data"]["nodes"]}
    assert rows["decide"]["mark"] == "ready" and (rows["seal"]["column"], rows["seal"]["row"]) == (7, 0)


def test_graph_show_refuses_a_run_of_another_graph_and_a_run_that_does_not_exist(cli, tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws")
    code, env = cli("run", "start", "daily", "--now", NOW, "--workspace", str(ws))
    run_id = env["data"]["run"]
    code, env = cli("graph", "show", "other", "--run", run_id, "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "run.graph_mismatch"
    assert env["error"]["details"] == {"run": run_id, "run_graph": "daily", "graph": "other"}
    code, env = cli("graph", "show", "daily", "--run", "2026-01-01-nope", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "run.not_found"
    code, env = cli("graph", "show", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"
    code, env = cli("graph", "show", "daily", "--width", "20", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"
