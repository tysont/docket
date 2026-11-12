"""``docket run diff`` through the CLI (TD §5.10): two runs of one workspace, a sealed sandbox
run and an open one, compared by topic. The command writes nothing."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Callable

from docket.cli import main

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
"""


def _item(i: str, title: str) -> dict[str, str]:
    return {"id": i, "title": title, "text": f"{title}, please.", "from": "pat@example.org",
            "at": "2026-10-15T09:00:00Z"}


REPORT, LAUNCH, BUDGET = "Send the quarterly report", "Review the launch plan", "Approve the travel budget"


def _call(ws: Path, argv: list[str]) -> dict[str, Any]:
    argv = [a for a in argv if a != "docket"]
    argv += [x for x in ("--json",) if x not in argv]
    if "--workspace" not in argv:
        argv += ["--workspace", str(ws)]
    out = io.StringIO()
    main(argv, stdout=out, stderr=io.StringIO(), cwd=str(ws))
    return json.loads(out.getvalue())


def _ok(ws: Path, argv: list[str]) -> dict[str, Any]:
    env = _call(ws, argv)
    assert env["ok"], (argv, env.get("error"))
    return env


def _drive(ws: Path, *, judge: dict[str, tuple[bool, int]], answer: dict[str, str],
           stop: Callable[[dict[str, Any]], bool] = lambda s: False) -> None:
    """Do every ready step until the run is sealed or ``stop`` matches a step. ``judge`` gives
    each title the assess keep and score; ``answer`` each title's option."""
    for _ in range(40):
        data = _ok(ws, ["next"])["data"]
        if data["done"]:
            return
        for step in data["steps"]:
            if stop(step):
                return
            if step["kind"] == "run":
                _ok(ws, step["command"]["argv"])
            elif step["kind"] == "dispatch":
                for t in step["tasks"]:
                    pack = json.loads(Path(t["pack_path"]).read_text(encoding="utf-8"))
                    rows = []
                    for it in pack["items"]:
                        keep, score = judge[it["title"]]
                        line = it["lines"][0]
                        rows.append({"id": it["id"], "source": it["source"], "keep": keep, "score": score,
                                     "kind": "request", "owner": "me", "ask": f"Follow up on {it['title']}",
                                     "evidence": [{"line": line["line"], "quote": line["text"][:200]}]})
                    Path(t["result_path"]).write_text(json.dumps({"items": rows}), encoding="utf-8")
                    _ok(ws, t["submit"]["argv"])
            elif step["kind"] == "ask":
                for q in step["questions"]:
                    opt = "yes" if q["kind"] == "approve" else next(v for k, v in answer.items() if k in q["prompt"])
                    argv = [q["id"] if a == "{question}" else opt if a == "{option}" else a
                            for a in step["answer"]["argv"]]
                    _ok(ws, argv)
            else:
                raise AssertionError(step)
    raise AssertionError("the run did not finish")


def _files(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


def _two_runs(tmp_path: Path, isolate: dict[str, Path]) -> tuple[Path, str, str]:
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    inbox = ws / "data" / "inbox.jsonl"
    inbox.write_text("".join(json.dumps(_item(i, t)) + "\n" for i, t in (("m1", REPORT), ("m2", LAUNCH))),
                     encoding="utf-8")
    (ws / "docket.yaml").write_text(WORKSPACE, encoding="utf-8")
    a = _ok(ws, ["run", "start", "daily", "--now", "2026-10-15T18:00:00+00:00", "--sandbox"])["data"]["run"]
    _drive(ws, judge={REPORT: (True, 70), LAUNCH: (True, 70)}, answer={REPORT: "task", LAUNCH: "task"})
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_item("m3", BUDGET)) + "\n")
    b = _ok(ws, ["run", "start", "daily", "--now", "2026-10-16T18:00:00+00:00"])["data"]["run"]
    _drive(ws, judge={REPORT: (True, 90), LAUNCH: (False, 20), BUDGET: (True, 70)},
           answer={REPORT: "dismiss", BUDGET: "task"}, stop=lambda s: s["node"] == "approve")
    return ws, a, b


DIFF = """\
A 2026-10-15-daily (sealed, sandbox)
B 2026-10-16-daily (open)

counts           A     B
  read           2     3   +1
  kept           2     2
  asked          2     2
  answered       2     2
  operations     2     1   -1

kept in one run only (2):
  kept in A only: Review the launch plan
    B: dropped: assess: the agent kept nothing (score 40); A: kept at score 57
  kept in B only: Approve the travel budget
    A: not read; B: kept at score 57

items that differ (1):
  Send the quarterly report: answer task -> dismiss; score 57 -> 63

operations that differ (1):
  backlog: 2 -> 1
    only in A: add `- [ ] Review the launch plan` (tasks.md under ## Inbox)
    only in A: add `- [ ] Send the quarterly report` (tasks.md under ## Inbox)
    only in B: add `- [ ] Approve the travel budget` (tasks.md under ## Inbox)"""


def test_run_diff_compares_a_sealed_sandbox_run_with_an_open_run(tmp_path: Path, isolate: dict[str, Path]) -> None:
    ws, a, b = _two_runs(tmp_path, isolate)
    before = _files(isolate["state"])
    out = io.StringIO()
    code = main(["run", "diff", a, b, "--workspace", str(ws)], stdout=out, stderr=io.StringIO(), cwd=str(ws))
    assert code == 0
    assert out.getvalue().rstrip("\n") == DIFF

    data = _ok(ws, ["run", "diff", a, b])["data"]
    assert data["a"] == {"run": a, "graph": "daily", "status": "sealed", "sandbox": True}
    assert data["b"]["status"] == "open" and data["b"]["sandbox"] is False
    assert data["counts"]["read"] == {"a": 2, "b": 3, "change": 1}
    assert [r["kept_in"] for r in data["kept"]["rows"]] == ["a", "b"]
    assert data["changed"]["rows"][0]["answer"] == {"a": "task", "b": "dismiss"}
    assert data["operations"]["rows"][0]["only_b"]["total"] == 1
    assert _files(isolate["state"]) == before  # read-only

    small = _ok(ws, ["run", "diff", a, b, "--limit", "1"])
    assert small["data"]["kept"]["total"] == 2 and len(small["data"]["kept"]["rows"]) == 1
    out = io.StringIO()
    main(["run", "diff", a, b, "--limit", "1", "--workspace", str(ws)], stdout=out, stderr=io.StringIO(), cwd=str(ws))
    assert "kept in one run only (2), first 1:" in out.getvalue()
    assert "... 1 more only in A" in out.getvalue()

    same = _ok(ws, ["run", "diff", a, a])["data"]
    assert same["same"] is True and same["kept"]["total"] == 0


def test_run_diff_refuses_a_run_that_does_not_exist(tmp_path: Path, isolate: dict[str, Path]) -> None:
    ws, a, _ = _two_runs(tmp_path, isolate)
    env = _call(ws, ["run", "diff", a, "2026-01-01-nope"])
    assert not env["ok"] and env["error"]["code"] == "run.not_found"
    assert a in env["error"]["details"]["known"]
    env = _call(ws, ["run", "diff", a, a, "--limit", "0"])
    assert env["error"]["code"] == "input.bad_argument"
