"""Graphs (TD §5.7): one run of several workflows and commands as a graph of tasks.

A graph's nodes are workflows, commands and refreshes. A node starts when the nodes it
``needs`` are done; a stage of a workflow node can wait on other nodes through ``gates``.
Nodes with nothing between them run at the same time: ``next_steps`` returns every step
that is ready, not one. A workflow node is an ordinary run with its own artifacts, lease
and apply boundary, so everything a run guarantees holds for it; the graph adds only when
it may start, what its stages wait for, and whether its plan is approved in advance.

Done means: a workflow node's run is sealed; a command or refresh node has run once. A
graph run is sealed when every node is done.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from docket.cli.errors import ExternalError, InputError, StateError
from docket.config.digest import digest
from docket.engine.steps import Command
from docket.store.runs import Clock, Store, create_run, load_run, new_run_id, wall_stamp
from docket.text import templates

PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")
DELEGATED = ("assess", "review", "label")
ASKED = ("decide", "approve")


# --- state ------------------------------------------------------------------------------


def graph_row(store: Store, grun: str) -> Any:
    row = store.db.execute("SELECT * FROM graph_runs WHERE graph_run_id=?", (grun,)).fetchone()
    if row is None:
        raise StateError("run.not_found", f"No graph run {grun!r}.", hint="docket graph start <graph>")
    return row


def latest_open(store: Store) -> str | None:
    row = store.db.execute("SELECT graph_run_id FROM graph_runs WHERE status='open' ORDER BY created_at DESC, "
                           "rowid DESC LIMIT 1").fetchone()
    return row["graph_run_id"] if row else None


def node_rows(store: Store, grun: str) -> dict[str, Any]:
    return {r["node"]: r for r in store.db.execute("SELECT * FROM graph_nodes WHERE graph_run_id=?", (grun,))}


def is_done(store: Store, row: Any) -> bool:
    if row["kind"] == "workflow":
        r = store.run_row(row["run_id"]) if row["run_id"] else None
        return r is not None and r["status"] == "sealed"
    return row["state"] == "done"


def unmet(ws: Any, store: Store, grun: str, node: str, stage: str, *, first: bool) -> list[str]:
    """The nodes ``stage`` of workflow node ``node`` still waits for."""
    try:
        g = ws.config.graphs[graph_row(store, grun)["graph"]]
    except (KeyError, StateError):
        return []
    n = g.nodes.get(node)
    if n is None:
        return []
    wanted = list(n.gates.get(stage, [])) + (list(n.needs) if first else [])
    rows = node_rows(store, grun)
    return sorted({w for w in wanted if w in rows and not is_done(store, rows[w])})


# --- start ------------------------------------------------------------------------------


def new_graph_run_id(store: Store, date: str, graph: str) -> str:
    base = f"{date}-{graph}"
    gid, n = base, 1
    while store.db.execute("SELECT 1 FROM graph_runs WHERE graph_run_id=?", (gid,)).fetchone() is not None:
        n += 1
        gid = f"{base}-{n}"
    return gid


def find_existing(store: Store, graph: str, date: str, sandbox: bool) -> str | None:
    row = store.db.execute("SELECT graph_run_id FROM graph_runs WHERE graph=? AND date=? AND sandbox=? AND status "
                           "IN ('open','sealed') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (graph, date, int(sandbox))).fetchone()
    return row["graph_run_id"] if row else None


def start(ws: Any, store: Store, graph: str, clock: Clock, *, sandbox: bool) -> str:
    """Create a graph run and a run for each workflow node (the caller holds the transaction).
    Every node shares the graph's pinned clock."""
    g = ws.config.graphs[graph]
    grun = new_graph_run_id(store, clock.date, graph)
    store.db.execute("INSERT INTO graph_runs (graph_run_id, graph, date, clock, sandbox, status, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (grun, graph, clock.date, clock.model_dump_json(), int(sandbox), "open",
                                                wall_stamp()))
    for nid, n in g.nodes.items():
        rid = None
        if n.workflow is not None:
            rid = new_run_id(store, clock.date, f"{graph}.{nid}")
            create_run(store, run_id=rid, workflow=n.workflow, clock=clock, sandbox=sandbox, graph_run=grun, node=nid)
        store.db.execute("INSERT INTO graph_nodes (graph_run_id, node, kind, run_id, state) VALUES (?,?,?,?,?)",
                         (grun, nid, n.kind, rid, "open"))
    return grun


# --- steps ------------------------------------------------------------------------------


def _argv(ws: Any, *words: str) -> list[str]:
    return ["docket", *words, "--workspace", str(ws.root), "--json"]


def _auto_approval(rc: Any, step: Any) -> list[str] | None:
    """The answer command that approves ``step`` in advance, when the node allows it: every
    operation of the plan must be for a private destination."""
    if getattr(step, "kind", None) != "ask" or getattr(step, "stage", None) != "approve":
        return None
    plan = rc.eval_or_none("plan")
    ops = list((plan.output or {}).get("operations") or []) if plan is not None else []
    dests = rc.ws.config.destinations
    if any(dests.get(o.get("destination")) is None or dests[o["destination"]].visibility != "private" for o in ops):
        return None
    q = step.questions[0]
    return rc.argv("ask", "answer", q.id, "yes", "--digest", str((q.details or {}).get("digest") or ""))


def next_steps(ctx: Any, grun: str, *, full: bool = False) -> dict[str, Any]:
    """Every step that is ready, the nodes that wait and why, and each node's state."""
    from docket.engine.context import RunContext
    from docket.engine.next import compute_step
    ws, store = ctx.workspace(), ctx.store()
    row = graph_row(store, grun)
    g = ws.config.graphs.get(row["graph"])
    if g is None:
        raise InputError("input.unknown_graph", f"Graph run {grun} uses graph {row['graph']!r}, which the workspace "
                                                f"no longer defines.", hint="docket config explain graphs")
    rows = node_rows(store, grun)
    steps: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    for nid, n in g.nodes.items():
        r = rows.get(nid)
        if r is None:
            continue
        view: dict[str, Any] = {"kind": n.kind, "run": r["run_id"], "state": "done" if is_done(store, r) else "open"}
        if r["detail"]:
            view["detail"] = json.loads(r["detail"])
        nodes[nid] = view
        if view["state"] == "done":
            continue
        waits = sorted(w for w in n.needs if w in rows and not is_done(store, rows[w]))
        if n.kind != "workflow":
            if waits:
                view["state"] = "waiting"
                blocked.append({"node": nid, "stage": None, "needs": waits})
                continue
            view["state"] = "ready"
            cmd = Command.of(_argv(ws, "graph", "exec", grun, nid))
            what = "runs its command" if n.kind == "command" else \
                f"reads {', '.join(n.refresh.sources)} again for node {n.refresh.node}"
            steps.append({"node": nid, "kind": "run", "stage": None, "command": cmd.model_dump(),
                          "why": f"node {nid} {what}; nothing it needs is outstanding",
                          "expect": "the node recorded as done", "timeout_s": int(n.timeout_s) + 30})
            continue
        rc = RunContext(ws, load_run(store, r["run_id"]), ctx.env)
        step = compute_step(rc, token=ctx.token(rc, create=False), full=full)
        if step.kind == "wait" and step.reason == "needs":
            ev = rc.first_pending()
            view["state"] = "waiting"
            blocked.append({"node": nid, "stage": step.stage, "needs": list((ev.extra if ev else {}).get("needs") or [])})
            continue
        if step.kind == "done":
            view["state"] = "done"
            continue
        view["state"] = "ready"
        auto = _auto_approval(rc, step) if n.approve == "auto" else None
        if auto is not None:
            steps.append({"node": nid, "run": rc.run.id, "kind": "run", "stage": "approve",
                          "command": Command.of(auto).model_dump(),
                          "why": f"node {nid} is approved in advance and every operation is for a private destination",
                          "expect": "the approval recorded against the plan digest", "timeout_s": 60})
            continue
        steps.append({"node": nid, "run": rc.run.id, **step.model_dump()})
    done = all(v["state"] == "done" for v in nodes.values())
    if done and row["status"] != "sealed":
        with store.db:
            store.db.execute("UPDATE graph_runs SET status='sealed' WHERE graph_run_id=?", (grun,))
    return {"graph_run": grun, "graph": row["graph"], "sandbox": bool(row["sandbox"]), "done": done,
            "status": "sealed" if done else row["status"], "steps": steps, "blocked": blocked, "nodes": nodes}


# --- commands and refreshes ------------------------------------------------------------


def exec_node(ctx: Any, grun: str, node: str) -> dict[str, Any]:
    """Run a command or refresh node once and record it done."""
    ws, store = ctx.workspace(), ctx.store()
    row = graph_row(store, grun)
    g = ws.config.graphs[row["graph"]]
    n = g.nodes.get(node)
    rows = node_rows(store, grun)
    if n is None or node not in rows or n.kind == "workflow":
        raise InputError("input.bad_argument", f"{node!r} is not a command or refresh node of graph run {grun}.",
                         hint=" ".join(_argv(ws, "graph", "status", grun)))
    if is_done(store, rows[node]):
        return {"node": node, "state": "done", "repeated": True, **json.loads(rows[node]["detail"] or "{}")}
    waits = sorted(w for w in n.needs if w in rows and not is_done(store, rows[w]))
    if waits:
        raise StateError("stage.not_ready", f"Node {node} waits for {', '.join(waits)}.",
                         hint=" ".join(_argv(ws, "graph", "next", grun)), details={"needs": waits})
    sandbox = bool(row["sandbox"])
    detail = _refresh(ctx, grun, n, rows) if n.kind == "refresh" else _command(ctx, grun, row, node, n, sandbox)
    with store.db:
        store.db.execute("UPDATE graph_nodes SET state='done', detail=? WHERE graph_run_id=? AND node=?",
                         (json.dumps(detail, sort_keys=True), grun, node))
    return {"node": node, "state": "done", "repeated": False, **detail}


def _command(ctx: Any, grun: str, row: Any, node: str, n: Any, sandbox: bool) -> dict[str, Any]:
    ws, store = ctx.workspace(), ctx.store()
    tpl = n.dry_run if sandbox else n.command
    if tpl is None:
        return {"outcome": "skipped", "message": "no dry_run form declared; skipped in a sandbox graph", "dry_run": True}
    clock = json.loads(row["clock"])
    gdir = store.root / "graphs" / grun
    gdir.mkdir(parents=True, exist_ok=True)
    argv = [templates.render(a, {"run": {"id": grun, "date": clock["date"], "dir": str(gdir),
                                          "sandbox": "true" if sandbox else ""}, "workspace": str(ws.root)}) for a in tpl]
    env = {k: ctx.env[k] for k in (*PASS_ENV, *n.env) if k in ctx.env}
    env.update({"DOCKET_NONINTERACTIVE": "1", "DOCKET_GRAPH_RUN": grun, "DOCKET_WORKSPACE": str(ws.root)})
    log = gdir / f"{node}.log"
    t0 = time.monotonic()
    with log.open("ab") as fh:
        fh.write(f"--- {node}: {' '.join(argv)}\n".encode())
        fh.flush()
        try:
            proc = subprocess.Popen(argv, cwd=str(ws.root), env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            code: int | None = proc.wait(timeout=n.timeout_s)
            msg = None if code == 0 else f"exited with status {code}"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.wait()
            code, msg = None, f"timed out after {n.timeout_s:g}s"
        except OSError as e:
            code, msg = None, f"could not start {argv[0]}: {e.strerror or e}"
    if code != 0:
        raise ExternalError("graph.node_failed", f"Node {node} {msg}.", retryable=True,
                            hint=" ".join(_argv(ws, "graph", "exec", grun, node)),
                            details={"node": node, "argv": argv, "exit": code, "log": str(log)})
    tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
    return {"outcome": "ok", "argv": argv, "dry_run": sandbox, "elapsed_s": round(time.monotonic() - t0, 3),
            "log": str(log), "last_line": tail[0][:400] if tail else ""}


def _refresh(ctx: Any, grun: str, n: Any, rows: dict[str, Any]) -> dict[str, Any]:
    """Open a new generation of the target node's collect for the named sources. A run that has
    not collected yet reads them in its first collect, so there is nothing to refresh."""
    import io
    from docket.cli import main
    target = rows[n.refresh.node]["run_id"]
    from docket.engine.context import RunContext
    rc = RunContext(ctx.workspace(), load_run(ctx.store(), target), ctx.env)
    ev = rc.eval_or_none("collect")
    if ev is None or ev.state != "current":
        return {"outcome": "ok", "refreshed": [], "message": "the node has not collected yet; its first collect reads them"}
    out = io.StringIO()
    argv = ["collect", "--refresh", "--sources", ",".join(n.refresh.sources), "--run", target, "--workspace",
            str(ctx.workspace().root), "--json"]
    code = main(argv, stdout=out, stderr=io.StringIO(), env=dict(ctx.env), cwd=str(ctx.cwd))
    body = json.loads(out.getvalue() or "{}")
    if code != 0:
        raise ExternalError("graph.node_failed", f"The refresh of {target} failed: "
                                                 f"{(body.get('error') or {}).get('message')}", retryable=True,
                            details={"argv": argv, "error": body.get("error")})
    return {"outcome": "ok", "refreshed": list(n.refresh.sources), "run": target}


# --- the graph as tasks ----------------------------------------------------------------


def tasks(ws: Any, graph: str) -> list[dict[str, Any]]:
    """The graph as tasks and what each waits for: a workflow node's stages one after another,
    the node's needs before its first, and each gate before its stage. ``by`` says who does
    a task: docket, an agent, the operator, or nobody (approved in advance)."""
    g = ws.config.graphs[graph]

    def last(nid: str) -> str:
        n = g.nodes[nid]
        return f"{nid}.{ws.config.workflows[n.workflow].stages[-1]}" if n.workflow is not None else nid

    out = []
    for nid, n in g.nodes.items():
        if n.workflow is None:
            out.append({"id": nid, "node": nid, "does": n.kind, "by": "docket", "needs": sorted(last(x) for x in n.needs)})
            continue
        prev: str | None = None
        for stage in ws.config.workflows[n.workflow].stages:
            needs = ([prev] if prev else [last(x) for x in n.needs]) + [last(x) for x in n.gates.get(stage, [])]
            by = "agent" if stage in DELEGATED else ("operator" if stage in ASKED else "docket")
            if stage == "approve" and n.approve == "auto":
                by = "in advance"
            out.append({"id": f"{nid}.{stage}", "node": nid, "does": stage, "by": by, "needs": sorted(set(needs))})
            prev = f"{nid}.{stage}"
    return out


def spec_digest(ws: Any, graph: str) -> str:
    return digest(ws.config.graphs[graph].model_dump(mode="json"))
