"""Graph commands (TD §5.7): start a graph run, get every ready step, run a command node."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError, StateError
from docket.cli.registry import Arg, command


def _graph_run(ctx: Any, ns: Any) -> str:
    from docket.engine import graph
    ctx.workspace_root()
    gid = getattr(ns, "id", None) or graph.latest_open(ctx.store())
    if not gid:
        raise StateError("run.none_open", "There is no open graph run.", hint="docket graph start <graph>")
    return gid


def _summary(data: dict[str, Any]) -> str:
    lines = [f"graph run {data['graph_run']} ({data['status']}): {len(data['steps'])} step(s) ready"]
    lines += [f"  {s['node']}: {s['kind']}" + (f" {s['stage']}" if s.get("stage") else "") for s in data["steps"]]
    lines += [f"  {b['node']}" + (f" {b['stage']}" if b.get("stage") else "") + f" waits for {', '.join(b['needs'])}"
              for b in data["blocked"]]
    return "\n".join(lines)


@command("graph start", "Create a run of a graph: a run for each workflow node, all on one pinned clock. Repeating "
         "it returns the same graph run.", mutating=True,
         args=[Arg("graph", "Graph name from docket.yaml."),
               Arg("--date", "Run date: today, tomorrow, a weekday name or YYYY-MM-DD."),
               Arg("--now", "Pin the clock to this ISO instant (rehearsal and tests)."),
               Arg("--sandbox", "Bind every destination to its sandbox twin and run commands in their dry form.",
                   action="store_true"),
               Arg("--new", "Create a new graph run even if one exists for this graph and date.", action="store_true")],
         effects=["creates a graph run and a run per workflow node", "takes each run's lease"],
         examples=["docket graph start morning --json", "docket graph start morning --sandbox --json"])
def cmd_graph_start(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import closest
    from docket.engine import graph
    from docket.engine.clock import pin
    from docket.engine.context import RunContext
    from docket.store.runs import load_run
    ws = ctx.workspace()
    if ns.graph not in ws.config.graphs:
        raise InputError("input.unknown_graph", f"No graph {ns.graph!r}.", hint="docket config explain graphs",
                         details={"suggestion": closest(ns.graph, list(ws.config.graphs)),
                                  "graphs": sorted(ws.config.graphs)})
    clock = pin(ws.config.profile.timezone, now=ns.now, on_date=ns.date)
    store = ctx.store()
    existing = None if ns.new else graph.find_existing(store, ns.graph, clock.date, bool(ns.sandbox))
    created = existing is None
    if created and ctx.dry_run:
        return Result({"dry_run": True, "would_create": graph.new_graph_run_id(store, clock.date, ns.graph)},
                      "would create a graph run")
    if created:
        with store.db:
            existing = graph.start(ws, store, ns.graph, clock, sandbox=bool(ns.sandbox))
        for row in graph.node_rows(store, existing).values():
            if row["run_id"]:
                ctx.acquire(RunContext(ws, load_run(store, row["run_id"]), ctx.env))
    data = graph.next_steps(ctx, existing)
    return Result({**data, "created": created}, ("started " if created else "") + _summary(data))


@command("graph next", "Every step of a graph run that is ready now, across its nodes. Run them together; a node "
         "that waits is listed with what it waits for.",
         args=[Arg("id", "Graph run id (default: the most recent open graph run).", nargs="?"),
               Arg("--full", "Give ask steps in full.", action="store_true")],
         examples=["docket graph next --json"])
def cmd_graph_next(ctx: Any, ns: Any) -> Result:
    from docket.engine import graph
    data = graph.next_steps(ctx, _graph_run(ctx, ns), full=bool(getattr(ns, "full", False)))
    return Result(data, _summary(data))


@command("graph status", "A graph run's nodes and their states.",
         args=[Arg("id", "Graph run id (default: the most recent open graph run).", nargs="?")],
         examples=["docket graph status --json"])
def cmd_graph_status(ctx: Any, ns: Any) -> Result:
    from docket.engine import graph
    data = graph.next_steps(ctx, _graph_run(ctx, ns))
    out = {k: data[k] for k in ("graph_run", "graph", "sandbox", "status", "done", "nodes", "blocked")}
    return Result(out, "\n".join([f"graph run {out['graph_run']}: {out['status']}"] +
                                  [f"  {n}: {v['state']}" for n, v in out["nodes"].items()]))


@command("graph exec", "Run a command or refresh node of a graph run, once.", mutating=True,
         args=[Arg("id", "Graph run id."), Arg("node", "Node name.")],
         effects=["runs the node's command (its dry form in a sandbox graph) or opens a refresh generation",
                  "records the node done"],
         examples=["docket graph exec 2026-10-15-morning prepare --json"])
def cmd_graph_exec(ctx: Any, ns: Any) -> Result:
    from docket.engine import graph
    ctx.workspace_root()
    if ctx.dry_run:
        return Result({"dry_run": True, "node": ns.node}, f"would run node {ns.node}")
    out = graph.exec_node(ctx, ns.id, ns.node)
    return Result(out, f"node {ns.node}: {out.get('outcome', out['state'])}")


@command("graph show", "A graph as tasks and what each waits for.",
         args=[Arg("graph", "Graph name from docket.yaml.")], examples=["docket graph show morning --json"])
def cmd_graph_show(ctx: Any, ns: Any) -> Result:
    from docket.engine import graph
    ws = ctx.workspace()
    if ns.graph not in ws.config.graphs:
        raise InputError("input.unknown_graph", f"No graph {ns.graph!r}.", hint="docket config explain graphs",
                         details={"graphs": sorted(ws.config.graphs)})
    rows = graph.tasks(ws, ns.graph)
    text = [f"{r['id']} ({r['by']})" + (f" <- {', '.join(r['needs'])}" if r["needs"] else "") for r in rows]
    return Result({"graph": ns.graph, "tasks": rows}, "\n".join(text))
