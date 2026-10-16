"""Agent tasks end to end (IP M4 exit, TD §9): refusals with per-row fixes, safe readings, review
verdicts in the pipeline, carry-forward across a refresh generation and across runs, dispatch
guidance and task schemas. All data is invented."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path

import pytest

from docket.items.mapping import item_id
from docket.store.paths import state_root
from tests.agent_loop import run_agent, synthetic_result

CASES = [("reference-like", "daily"), ("support-lead", "shift")]


def start(cli, ws, workflow, pinned_now, *extra):
    code, env = cli("run", "start", workflow, "--now", pinned_now, "--workspace", str(ws), *extra)
    assert code == 0, env
    return env["data"]["run"]


def submit(cli, task, result):
    Path(task["result_path"]).write_text(json.dumps(result), encoding="utf-8")
    return cli(*task["submit"]["argv"][1:])


def run_dir(ws, run_id):
    return state_root(ws, os.environ) / "runs" / run_id


def artifact(ws, run_id, stage):
    d = run_dir(ws, run_id) / "artifacts" / stage
    return json.loads((d / ((d / "latest").read_text().strip() + ".json")).read_text())["data"]


@pytest.mark.parametrize("name,workflow", CASES)
def test_exit_scripted_submission_refuses_three_rows_and_accepts_the_safe_reading(persona, cli, pinned_now,
                                                                                    name, workflow):
    ws = persona(name)
    run_id = start(cli, ws, workflow, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "dispatch")
    assert res.failures == []
    task = next(t for t in res.final["tasks"] if t["kind"] == "assess")
    pack = json.loads(Path(task["pack_path"]).read_text())
    good = synthetic_result("assess", pack)
    rows = copy.deepcopy(good["items"])
    wrong_source, paraphrased, no_owner, odd_kind = rows[0]["id"], rows[1]["id"], rows[2]["id"], rows[3]["id"]
    rows[0]["source"] = "elsewhere"
    rows[1]["evidence"] = [{"line": "L1", "quote": "In short, they want an answer on this soon."}]
    del rows[2]["owner"]
    rows[3]["kind"] = "hot-potato"

    code, env = submit(cli, task, {"items": rows})
    assert code == 4 and env["error"]["code"] == "task.rows_refused" and env["error"]["kind"] == "integrity"
    details = env["error"]["details"]
    assert details["refused"] == sorted([wrong_source, paraphrased, no_owner])
    by_id = {r["id"]: r for r in details["rows"]}
    assert by_id[wrong_source]["code"] == "task.identity_mismatch" and by_id[wrong_source]["path"] == "items[0].source"
    assert by_id[paraphrased]["code"] == "task.evidence_not_verbatim"
    assert by_id[paraphrased]["path"] == "items[1].evidence[0].quote"
    assert by_id[no_owner]["code"] == "task.cross_field" and by_id[no_owner]["path"] == "items[2].owner"
    for r in details["rows"]:
        assert {"id", "path", "received", "rule", "fix", "code"} <= set(r) and r["fix"] and r["rule"]
    assert details["rerun"] == task["submit"]["shell"] == env["error"]["hint"]
    assert env["unexpected"] == []  # a refused result records nothing
    assert not (run_dir(ws, run_id) / "tasks" / task["task_id"] / "result.json").exists()

    for i in (0, 1, 2):
        rows[i] = good["items"][i]
    code, env = submit(cli, task, {"items": rows})
    assert code == 0 and env["data"]["accepted"] is True, env
    shapes = [s for s in env["unexpected"] if s["area"] == "task.vocabulary"]
    assert shapes == [{"area": "task.vocabulary", "detail": "assess.kind unknown value", "reading": "read as fyi",
                       "extend": "tasks.assess.kinds: values", "sample": "hot-potato"}]
    result = json.loads((run_dir(ws, run_id) / "tasks" / task["task_id"] / "result.json").read_text())
    assert next(r for r in result["items"] if r["id"] == odd_kind)["kind"] == "fyi"
    journal = (run_dir(ws, run_id) / "unexpected.jsonl").read_text()
    assert "assess.kind unknown value" in journal
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    assert db.execute("SELECT COUNT(*) FROM unexpected WHERE detail='assess.kind unknown value'").fetchone()[0] == 1
    assert db.execute("SELECT kind, task_id FROM task_results WHERE input_digest=?",
                      (pack["input_digest"],)).fetchone() == ("assess", task["task_id"])
    db.close()
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"


def test_review_verdicts_drop_merge_and_flag_items(persona, cli, pinned_now):
    ws = persona("reference-like")
    run_id = start(cli, ws, "daily", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "dispatch" and s["stage"] == "review")
    assert res.failures == []
    tasks = res.final["tasks"]
    first, rest = tasks[0], tasks[1:]
    pack = json.loads(Path(first["pack_path"]).read_text())
    assert all("score" not in it and "proposal" in it for it in pack["items"])
    rows = synthetic_result("review", pack)["items"]
    dropped, merged, target, flagged = (r["id"] for r in rows[:4])
    rows[0] = {"id": dropped, "verdict": "drop", "reason": "Already handled in the thread."}
    rows[1] = {"id": merged, "verdict": "merge", "merge_with": target, "reason": "Same request as another."}
    rows[3] = {"id": flagged, "verdict": "needs_verification", "reason": "The pack cannot tell who owns it."}
    code, env = submit(cli, first, {"items": rows})
    assert code == 0, env
    for t in rest:
        code, env = submit(cli, t, synthetic_result("review", json.loads(Path(t["pack_path"]).read_text())))
        assert code == 0, env
    items = {it["id"]: it for it in artifact(ws, run_id, "review")["items"]}
    assert items[dropped]["keep"] is False and items[dropped]["dropped"] == {"stage": "review",
                                                                            "reason": "Already handled in the thread."}
    assert items[dropped]["trace"][-1]["verdict"] == "drop" and items[dropped]["trace"][-1]["stage"] == "review"
    assert items[merged]["keep"] is False and items[merged]["merged_into"] == target
    assert items[target]["merged"] == [merged] and items[target]["keep"] is True
    assert items[flagged]["needs_verification"] is True and items[flagged]["keep"] is True
    assert items[target]["review"]["against"]
    code, env = cli("ask", "list", "--workspace", str(ws))
    asked = {q["id"] for q in env["data"]["questions"]}
    assert f"q_{dropped}" not in asked and f"q_{merged}" not in asked


REVIEWED = """\
  reviewed:
    stages: [collect, assess, merge, review, decide, plan, approve, apply, close]
    sources: [tickets]
    questions: { kinds: [finding], budget: 40 }
    options: queue
"""


def test_review_verdicts_carry_forward_across_a_refresh_generation(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("workflows:\n", "workflows:\n" + REVIEWED, 1))
    run_id = start(cli, ws, "reviewed", pinned_now)
    first = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert first.failures == [] and first.final["stage"] == "decide"
    reviewed0 = [it["id"] for s in first.steps if s["kind"] == "dispatch" and s["stage"] == "review"
                 for t in s["tasks"] for it in json.loads(Path(t["pack_path"]).read_text())["items"]]
    # Every kept ticket is reviewed once; signal rules decide how many are kept.
    assert reviewed0 and len(reviewed0) == len(set(reviewed0))

    feed = ws / "data" / "tickets.jsonl"
    lines = feed.read_text().splitlines()
    changed = json.loads(lines[0])
    with feed.open("a") as fh:
        fh.write(json.dumps({**changed, "key": "SUP-2001", "summary": "Arrived after the evening collect"}) + "\n")
        fh.write(json.dumps({**changed, "description": "Customer report 0: the customer replied with a new date."})
                 + "\n")
    code, env = cli("collect", "--refresh", "--sources", "tickets", "--workspace", str(ws))
    assert code == 0 and env["data"]["generation"] == 1, env
    second = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert second.failures == []
    review_steps = [s for s in second.steps if s["kind"] == "dispatch" and s["stage"] == "review"]
    reviewed1 = sorted(it["id"] for s in review_steps for t in s["tasks"]
                       for it in json.loads(Path(t["pack_path"]).read_text())["items"])
    assert reviewed1 == sorted([item_id("tickets", "", "SUP-2001"), item_id("tickets", "", changed["key"])])
    items = artifact(ws, run_id, "review")["items"]
    carried = [it for it in items if it.get("trace") and it["trace"][-1].get("carried")]
    assert len(carried) == len(reviewed0) - 1 and all(it["verdict"] == "ask" for it in carried)
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"


def test_accepted_results_are_reused_by_input_digest_in_a_later_run(persona, cli, pinned_now):
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now, "--sandbox")
    one = run_agent(ws)
    assert one.final["kind"] == "done" and any(s["kind"] == "dispatch" for s in one.steps)
    second = start(cli, ws, "shift", "2026-10-16T18:00:00+01:00", "--sandbox", "--date", "2026-10-17")
    two = run_agent(ws)
    assert two.failures == [] and two.final["kind"] == "done"
    assert not any(s["kind"] == "dispatch" for s in two.steps)  # same packs: no task is issued again
    code, env = cli("task", "list", "--workspace", str(ws), "--run", second)
    assert code == 0 and env["data"]["tasks"] and all(t["status"] == "accepted" for t in env["data"]["tasks"])


def test_dispatch_guidance_concurrency_inline_and_exact_commands(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("tasks:\n  assess: { max_items: 8 }", "tasks:\n  concurrency: 3\n"
                                                                                "  assess: { max_items: 8 }"))
    start(cli, ws, "shift", pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "dispatch")
    step = res.final
    assert step["concurrency"] == 3 and "inline" in step["why"] and "independent" in step["why"]
    kinds = {t["kind"] for t in step["tasks"]}
    assert kinds == {"assess", "sla_check"}
    for t in step["tasks"]:
        assert t["submit"]["argv"][1:4] == ["task", "submit", t["task_id"]] and t["result_path"] in t["submit"]["argv"]
        assert t["result_schema"] == f"{t['kind']}-output"
        prompt = Path(t["prompt_path"]).read_text()
        assert t["submit"]["shell"] in prompt and "<" not in t["submit"]["shell"]
    code, env = cli("next", "--workspace", str(ws))
    assert len(json.dumps(env)) < 20000  # paths, not contents (AE-08)


def test_task_kinds_and_schemas_for_every_kind(persona, cli, tmp_path):
    ws = persona("reference-like")
    code, env = cli("task", "kinds", "--workspace", str(ws))
    assert code == 0
    kinds = {k["kind"]: k for k in env["data"]["kinds"]}
    assert set(kinds) == {"assess", "extract", "review", "classify", "tone"}
    assert kinds["tone"]["issued"] is True and kinds["extract"]["issued"] is False
    for k in kinds.values():
        for name in (k["input_schema"], k["output_schema"]):
            code, env = cli("schema", name, "--workspace", str(ws))
            assert code == 0 and env["data"]["schema"]["type"] == "object", name
    code, env = cli("schema", "tone-output", "--workspace", str(ws))
    rows = env["data"]["schema"]["$defs"]["ToneRow"]["properties"]
    assert rows["tone"]["x-meanings"]["urgent"] == "Wants action now." and rows["tone"]["x-safe"] == "calm"
    code, env = cli("schema", "tone-output", cwd=tmp_path)
    assert code == 2 and env["error"]["code"] == "input.unknown_schema"
    code, env = cli("schema", "assess-output", cwd=tmp_path)
    assert code == 0 and "x-rules" in json.dumps(env["data"]["schema"])


def test_evidence_is_checked_against_the_latest_generation_of_a_record(persona, cli, pinned_now):
    ws = persona("support-lead")
    start(cli, ws, "shift", pinned_now)
    code, env = cli("collect", "--workspace", str(ws))
    assert code == 0
    feed = ws / "data" / "tickets.jsonl"
    # The first ticket an agent assesses: P1 and P2 tickets are rules-only (no task).
    first = next(r for r in map(json.loads, feed.read_text().splitlines()) if r["priority"] not in ("P1", "P2"))
    with feed.open("a") as fh:
        fh.write(json.dumps({**first, "description": "Customer report 0: resolved by the refund team."}) + "\n")
    code, env = cli("collect", "--refresh", "--sources", "tickets", "--workspace", str(ws))
    assert code == 0
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "dispatch")
    target = item_id("tickets", "", first["key"])
    task, pack = next((t, p) for t in res.final["tasks"] if t["kind"] == "assess"
                      for p in [json.loads(Path(t["pack_path"]).read_text())]
                      if target in [it["id"] for it in p["items"]])
    rows = synthetic_result("assess", pack)["items"]
    row = next(r for r in rows if r["id"] == target)
    assert row["evidence"][0]["quote"] == "Customer report 0: resolved by the refund team."
    row["evidence"] = [{"line": "L1", "quote": first["description"]}]  # the superseded text
    code, env = submit(cli, task, {"items": rows})
    assert code == 4 and [r["code"] for r in env["error"]["details"]["rows"]] == ["task.evidence_not_verbatim"]
    code, env = submit(cli, task, synthetic_result("assess", pack))
    assert code == 0, env
