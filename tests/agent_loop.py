"""A minimal agent (Test Plan §6.1).

It loops on ``docket next --json`` and uses only fields from the step:

- ``run``: call the step's argv (without the leading ``docket``);
- ``dispatch``: read each pack, write a synthetic result that satisfies the
  real validators to the task's ``result_path`` (assess: keep all, score 70,
  owner me, an ask, and the item's first line quoted verbatim as evidence;
  review: verdict ask with an argument against; any other kind: the example
  row of the task's prompt for every item, with identity copied and evidence
  quoted from the item) and call the task's ``submit`` argv;
- ``ask``: answer each question with its first option's argv (the approve
  question's ``yes`` argv carries the plan digest);
- ``wait``: when the reason is ``paused`` and resuming is allowed, call the
  step's ``command`` (the documented resume command); otherwise stop;
- ``done``: stop.

With ``repeat_commands`` every command the agent runs (other than ``next``) is
run twice, as if the agent had been killed after the first call and repeated it.
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from docket.cli import main


@dataclass
class LoopResult:
    final: dict[str, Any]
    steps: list[dict[str, Any]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


def _quote(item: dict[str, Any]) -> list[dict[str, str]]:
    line = item["lines"][0]
    return [{"line": line["line"], "quote": line["text"][:200]}]


def prompt_example(prompt: str) -> dict[str, Any]:
    """The example row embedded in a prompt's output specification."""
    block = prompt.split("## Output specification", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)["items"][0]


def _fill(template: Any, item: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {k: (_quote(item) if k == "evidence" or (isinstance(v, list) and v and isinstance(v[0], dict)
                                                        and "quote" in v[0]) else _fill(v, item))
                for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, item) for v in template]
    return template


def synthetic_result(kind: str, pack: dict[str, Any], prompt: str = "") -> dict[str, Any]:
    if kind == "assess":
        return {"items": [{"id": it["id"], "source": it["source"], "keep": True, "score": 70, "kind": "request",
                           "owner": "me", "ask": f"Follow up on {it['title'][:80]}".strip(),
                           "evidence": _quote(it)} for it in pack["items"]]}
    if kind == "review":
        return {"items": [{"id": it["id"], "verdict": "ask", "reason": "synthetic",
                           "against": "Someone else may already be handling it."} for it in pack["items"]]}
    example = prompt_example(prompt)
    rows = []
    for it in pack["items"]:
        row = _fill(example, it)
        row["id"] = it["id"]
        if "source" in row:
            row["source"] = it["source"]
        rows.append(row)
    return {"items": rows}


def run_agent(workspace: Path, *, env: dict[str, str] | None = None, cwd: Path | None = None,
              resume_paused: bool = True, max_steps: int = 200,
              choose: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
              stop_at: Callable[[dict[str, Any]], bool] | None = None,
              repeat_commands: bool = False) -> LoopResult:
    environ = dict(os.environ if env is None else env)
    workdir = str(cwd or workspace)
    res = LoopResult(final={})

    def call(argv: list[str], *, again: bool = True) -> dict[str, Any]:
        if argv and argv[0] == "docket":
            argv = argv[1:]
        if repeat_commands and again and argv[:1] != ["next"]:
            first = call(argv, again=False)
            if not first["ok"]:
                return first
        if "--json" not in argv:
            argv = argv + ["--json"]
        out = io.StringIO()
        code = main(argv, stdout=out, stderr=io.StringIO(), env=environ, cwd=workdir)
        res.calls.append(argv)
        env_out = json.loads(out.getvalue())
        if code != 0:
            res.failures.append({"argv": argv, "code": code, "error": env_out.get("error")})
        return env_out

    for _ in range(max_steps):
        nxt = call(["next", "--workspace", str(workspace)])
        if not nxt["ok"]:
            res.final = {"kind": "error", "error": nxt["error"]}
            return res
        step = nxt["data"]["step"]
        res.steps.append(step)
        if stop_at is not None and stop_at(step):
            res.final = step
            return res
        kind = step["kind"]
        if kind == "run":
            call(step["command"]["argv"])
        elif kind == "dispatch":
            for t in step["tasks"]:
                pack = json.loads(Path(t["pack_path"]).read_text(encoding="utf-8"))
                prompt = Path(t["prompt_path"]).read_text(encoding="utf-8")
                result = synthetic_result(t["kind"], pack, prompt)
                Path(t["result_path"]).write_text(json.dumps(result), encoding="utf-8")
                call(t["submit"]["argv"])
        elif kind == "ask":
            for q in step["questions"]:
                opt = choose(q) if choose else q["options"][0]
                call(opt["answer"]["argv"])
        elif kind == "wait":
            if resume_paused and step["reason"] == "paused" and step.get("command"):
                call(step["command"]["argv"])
                continue
            res.final = step
            return res
        elif kind == "done":
            res.final = step
            return res
        if res.failures:
            res.final = {"kind": "error", "error": res.failures[-1]}
            return res
    res.final = {"kind": "error", "error": "max_steps exceeded"}
    return res
