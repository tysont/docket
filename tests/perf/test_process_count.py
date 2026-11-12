"""No process per item (TP §10, AE-10, TD §18), and a full synthetic run per persona under 60 s (NFR-06).

A synthetic workspace reads N invented items from a files source, assesses them by rules,
asks a capped number of questions, applies to a Markdown task store, runs one command node
after the apply, and seals.
Every subprocess launch (``subprocess.Popen`` and ``asyncio.create_subprocess_exec``) is
counted; the count must not grow from 50 to 500 items.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

from tests.agent_loop import run_agent

pytestmark = pytest.mark.slow

WORKSPACE = """\
version: 1
graphs:
  daily:
    read-feed: { read: feed }
    assess: { judge: assess, from: [read-feed] }
    merge: { merge: [assess] }
    decide: { ask: default, from: [merge], questions: { kinds: [finding], budget: 5, overflow: drop } }
    plan: { plan: [decide] }
    approve: { approve: plan }
    apply: { apply: approve }
    notes-commit: { run: [docket-stub-auth, --ok], after: [apply] }
profile:
  timezone: UTC
  workdays: [mon, tue, wed, thu, fri]
  me: { name: Pat Example }
sources:
  feed:
    kind: files
    path: data/items.jsonl
    mapping: { id: "$.key", title: "$.summary", text: "$.body", at: "$.created" }
    auth: { check: [docket-stub-auth, --ok] }
signals:
  rules:
    - id: queue
      when: { source: feed }
      then: { assess: rules_only, importance: +40, urgency: +20 }
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
    section: "## Inbox"
"""


def workspace(root: Path, n: int) -> Path:
    root.mkdir(parents=True)
    (root / "docket.yaml").write_text(WORKSPACE)
    (root / "tasks.md").write_text("# Tasks\n\n## Inbox\n")
    (root / "data").mkdir()
    rows = [json.dumps({"key": f"ITEM-{i:04d}", "summary": f"Invented request number {i}",
                        "body": f"Please look at invented request {i} before the weekly review.",
                        "created": "2026-10-15T09:00:00Z"}) for i in range(n)]
    (root / "data" / "items.jsonl").write_text("\n".join(rows) + "\n")
    return root


def launches(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_init = subprocess.Popen.__init__
    real_exec = asyncio.create_subprocess_exec

    def init(self, args, *a, **kw):
        seen.append(str(args[0] if isinstance(args, (list, tuple)) else args))
        return real_init(self, args, *a, **kw)

    async def create(program, *a, **kw):
        seen.append(str(program))
        return await real_exec(program, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", init)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return seen


def full_run(cli, ws: Path, now: str, graph: str = "daily") -> dict:
    code, env = cli("run", "start", graph, "--now", now, "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    return res.final["summary"]


def test_subprocess_launches_stay_constant_from_50_to_500_items(cli, tmp_path, monkeypatch, pinned_now):
    seen = launches(monkeypatch)
    counts = {}
    for n in (50, 500):
        seen.clear()
        ws = workspace(tmp_path / f"ws{n}", n)
        summary = full_run(cli, ws, pinned_now)
        counts[n] = len(seen)
        assert summary["items"] == n and summary["applied"] == 5
    print(f"subprocess launches: {counts}")
    assert counts[50] == counts[500] and counts[50] >= 2  # the auth check and the run node, nothing per item


@pytest.mark.parametrize("name,graph", [("reference-like", "daily"), ("support-lead", "shift")])
def test_full_synthetic_run_under_60_seconds(persona, cli, pinned_now, name, graph):
    ws = persona(name)
    t0 = time.perf_counter()
    full_run(cli, ws, pinned_now, graph)
    elapsed = time.perf_counter() - t0
    print(f"{name} full synthetic run {elapsed:.1f}s")
    assert elapsed < 60
