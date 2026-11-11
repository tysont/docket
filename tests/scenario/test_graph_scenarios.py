"""Graphs (TD §5.7): one run of several workflows and commands as a graph of tasks. A node
starts as soon as what it needs is done, so independent work overlaps; a stage inside a
workflow node can wait on another node; and a node may be approved in advance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.scenario.test_agenda_roll_scenario import setup as setup_agendas

GRAPH = """
graphs:
  morning:
    description: Get the notes ready, then triage.
    nodes:
      prepare: { command: [touch, "{run.dir}/prepared-{run.date}"], dry_run: [touch, "{run.dir}/rehearsed"] }
      agendas: { workflow: agendas, approve: auto }
      triage:
        workflow: triage
        gates: { decide: [prepare, agendas] }
"""


@pytest.fixture
def ws(persona):
    w = persona("reference-like")
    setup_agendas(w)
    cfg = w / "docket.yaml"
    cfg.write_text(cfg.read_text() + GRAPH)
    return w


def synthetic(step):
    from tests.agent_loop import synthetic_result
    for t in step["tasks"]:
        pack = json.loads(Path(t["pack_path"]).read_text())
        prompt = Path(t["prompt_path"]).read_text()
        Path(t["result_path"]).write_text(json.dumps(synthetic_result(t["kind"], pack, prompt)))


def drive(cli, ws, grun, *, only=None, limit=60, stop=None):
    """Run every ready step, round after round, the way a driver does. Returns each round's
    steps as (node, kind, stage)."""
    rounds = []
    for _ in range(limit):
        code, env = cli("graph", "next", grun, "--workspace", str(ws))
        assert code == 0, env
        data = env["data"]
        steps = [s for s in data["steps"] if only is None or s["node"] in only]
        rounds.append([(s["node"], s["kind"], s.get("stage")) for s in data["steps"]])
        if data["done"] or not steps or (stop and stop(data)):
            return rounds, data
        for s in steps:
            if s["kind"] == "run":
                c, e = cli(*s["command"]["argv"][1:])
                assert c == 0, e
            elif s["kind"] == "dispatch":
                synthetic(s)
                for t in s["tasks"]:
                    c, e = cli(*t["submit"]["argv"][1:])
                    assert c == 0, e
            elif s["kind"] == "ask":
                for q in s["questions"]:
                    opt = q["options"][0]
                    argv = [a.replace("{question}", q["id"]).replace("{option}", opt["id"]) for a in s["answer"]["argv"][1:]]
                    c, e = cli(*argv)
                    assert c == 0, e
    raise AssertionError("the graph did not finish")


def test_a_graph_is_checked_when_the_workspace_loads(ws, cli):
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    cfg = ws / "docket.yaml"
    good = cfg.read_text()
    for bad, word in ((good.replace("gates: { decide: [prepare, agendas] }", "gates: { decide: [nobody] }"), "nobody"),
                      (good.replace("agendas: { workflow: agendas, approve: auto }", "agendas: { workflow: nowhere }"), "nowhere"),
                      (good.replace("prepare: { command:", "prepare: { needs: [triage], command:"), "cycle"),
                      (good.replace("gates: { decide:", "gates: { label:"), "label")):
        cfg.write_text(bad)
        code, env = cli("config", "validate", "--workspace", str(ws))
        assert code != 0 and word in json.dumps(env), word
    cfg.write_text(good)


def test_independent_nodes_start_together_and_a_gated_stage_waits(ws, cli, pinned_now):
    code, env = cli("graph", "start", "morning", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    grun = env["data"]["graph_run"]
    assert set(env["data"]["nodes"]) == {"prepare", "agendas", "triage"}
    code, env = cli("graph", "next", grun, "--workspace", str(ws))
    first = {(s["node"], s["kind"], s.get("stage")) for s in env["data"]["steps"]}
    # nothing waits on anything yet: the command and both workflows' first steps are all ready
    assert first == {("prepare", "run", None), ("agendas", "run", "collect"), ("triage", "run", "collect")}

    # drive only triage: it reads, assesses and merges, then stops at the gate
    rounds, data = drive(cli, ws, grun, only={"triage"})
    assert [s for r in rounds for s in r if s[0] == "triage" and s[2] == "decide"] == []
    (blocked,) = [b for b in data["blocked"] if b["node"] == "triage"]
    assert blocked["stage"] == "decide" and sorted(blocked["needs"]) == ["agendas", "prepare"]
    triage_run = env["data"]["nodes"] if False else data["nodes"]["triage"]["run"]
    code, env = cli("decide", "--run", triage_run, "--workspace", str(ws)) if False else cli("next", "--run", triage_run, "--workspace", str(ws))
    assert env["data"]["step"]["kind"] == "wait" and env["data"]["step"]["reason"] == "needs"    # the run itself says so

    # finish the other two; the agendas node is approved in advance, so nobody is asked
    rounds, data = drive(cli, ws, grun, only={"prepare", "agendas"},
                         stop=lambda d: all(d["nodes"][n]["state"] == "done" for n in ("prepare", "agendas")))
    asked = [s for r in rounds for s in r if s[0] == "agendas" and s[1] == "ask"]
    assert asked == [] and data["nodes"]["agendas"]["state"] == "done" and data["nodes"]["prepare"]["state"] == "done"
    assert "## 2026-10-14" in (ws / "Syncs" / "Platform.md").read_text()                 # its writes are applied
    # now the gate is open and triage asks its questions
    code, env = cli("graph", "next", grun, "--workspace", str(ws))
    assert ("triage", "ask", "decide") in {(s["node"], s["kind"], s.get("stage")) for s in env["data"]["steps"]}
    rounds, data = drive(cli, ws, grun)
    assert data["done"] and all(n["state"] == "done" for n in data["nodes"].values())
    code, env = cli("graph", "status", grun, "--workspace", str(ws))
    assert env["data"]["status"] == "sealed"


def test_a_rehearsal_of_a_graph_writes_nothing_and_runs_the_dry_form_of_a_command(ws, cli, pinned_now):
    note = (ws / "Syncs" / "Platform.md").read_text()
    code, env = cli("graph", "start", "morning", "--sandbox", "--now", pinned_now, "--workspace", str(ws))
    grun = env["data"]["graph_run"]
    rounds, data = drive(cli, ws, grun)
    assert data["done"]
    assert (ws / "Syncs" / "Platform.md").read_text() == note
    logs = json.dumps(data["nodes"]["prepare"])
    assert "rehearsed" in logs and "prepared-" not in logs


def test_the_graph_can_be_shown_as_tasks_and_what_each_waits_for(ws, cli):
    code, env = cli("graph", "show", "morning", "--workspace", str(ws))
    assert code == 0, env
    nodes = {n["id"]: n for n in env["data"]["tasks"]}
    assert nodes["prepare"]["needs"] == [] and nodes["agendas.collect"]["needs"] == []
    assert nodes["agendas.assess"]["needs"] == ["agendas.collect"]
    assert sorted(nodes["triage.decide"]["needs"]) == ["agendas.close", "prepare", "triage.merge"]
    assert nodes["triage.decide"]["by"] == "operator" and nodes["triage.assess"]["by"] == "agent"
    assert nodes["agendas.approve"]["by"] == "in advance" and nodes["triage.approve"]["by"] == "operator"


def test_a_delegated_stage_can_be_gated(ws, cli, pinned_now):
    """A stage whose tasks are done by agents can wait on another node: the stage before it
    finishes cleanly, and no packs are written until the gate opens."""
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("gates: { decide: [prepare, agendas] }", "gates: { assess: [prepare] }"))
    code, env = cli("graph", "start", "morning", "--now", pinned_now, "--workspace", str(ws))
    grun = env["data"]["graph_run"]
    rounds, data = drive(cli, ws, grun, only={"triage"})
    (blocked,) = [b for b in data["blocked"] if b["node"] == "triage"]
    assert blocked["stage"] == "assess" and blocked["needs"] == ["prepare"]
    rounds, data = drive(cli, ws, grun)
    assert data["done"]
