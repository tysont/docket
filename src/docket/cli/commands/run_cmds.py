"""Run commands and the loop entry point: ``next``, ``run start|status|list|continue|steal|abandon``
and ``graph show``."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError, StateError
from docket.cli.registry import Arg, command


def steps_next(steps: list[Any]) -> tuple[list[str], str] | None:
    """The envelope's next command: the first step that is a command to run."""
    for s in steps:
        if getattr(s, "kind", None) == "run":
            return s.command.argv, s.why
    return None


def step_text(step: Any) -> str:
    k = step.kind
    if k == "run":
        return f"{step.node}: run {step.command.shell}\n  why: {step.why}"
    if k == "dispatch":
        return f"{step.node}: dispatch {len(step.tasks)} task(s); concurrency {step.concurrency}"
    if k == "ask":
        lines = [f"{step.node}: ask {len(step.questions)} question(s):"]
        for q in step.questions[:20]:
            opts = ", ".join(f"{i}={o.id}" for i, o in enumerate(q.options, 1))
            lines.append(f"  {q.id}: {q.prompt} [{opts}]")
        if len(step.questions) > 20:
            lines.append(f"  ... {len(step.questions) - 20} more")
        lines += [f"  answer: {step.answer.shell}", f"  details: {step.show.shell}"]
        return "\n".join(lines)
    return f"{step.node or 'run'}: wait ({step.reason}): {step.instructions}"


def steps_summary(steps: list[Any]) -> str:
    if not steps:
        return "nothing is ready"
    return f"{len(steps)} step(s) ready:\n" + "\n".join(step_text(s) for s in steps)


def steps_brief(rc: Any, steps: list[Any]) -> list[dict[str, Any]]:
    """The ready steps of a mutation response (AE-08): each one's node, kind and counts, never
    a whole burst. ``docket next`` returns the steps themselves."""
    out = []
    for step in steps:
        row: dict[str, Any] = {"kind": step.kind, "node": getattr(step, "node", None)}
        if step.kind == "run":
            row.update(command=step.command.model_dump(), why=step.why)
        elif step.kind == "ask":
            row.update(counts={"questions": len(step.questions)}, question_ids=[q.id for q in step.questions])
        elif step.kind == "dispatch":
            row.update(counts={"tasks": len(step.tasks)})
        else:
            row.update(reason=step.reason, command=step.command.model_dump() if step.command else None)
        out.append(row)
    return out


def brief_text(briefs: list[dict[str, Any]]) -> str:
    def one(b: dict[str, Any]) -> str:
        return {"ask": lambda: f"ask {b['counts']['questions']} question(s)",
                "dispatch": lambda: f"dispatch {b['counts']['tasks']} task(s)",
                "wait": lambda: f"wait ({b['reason']})", "run": lambda: "run"}[b["kind"]]() + f" ({b['node']})"
    return "next: " + ("; ".join(one(b) for b in briefs) if briefs else "nothing is ready (docket next)")


@command("next", "Return every step that is ready now: run, dispatch, ask or wait, each naming its node. Steps "
                 "of nodes that do not depend on each other are ready together; do them together. An ask step is "
                 "compact: option ids and labels, one answer template and a show template per step.", run=True,
         args=[Arg("--full", "Ask steps: give every option its own answer command and every question its "
                             "evidence, defaults and refused options.", action="store_true")],
         effects=["renews the lease heartbeat when this session holds it; nothing else"],
         examples=["docket next --json", "docket next --full --json"])
def cmd_next(ctx: Any, ns: Any) -> Result:
    from docket.engine.next import compute_steps, envelope, summary
    from docket.engine.steps import Command
    from docket.store import lease
    store = ctx.store()
    if not getattr(ns, "run", None) and store.latest_open() is None:
        ws = ctx.workspace()
        graphs = sorted(ws.config.graphs)
        cmd = ["docket", "run", "start", graphs[0], "--workspace", str(ws.root), "--json"] if graphs else None
        runs = store.list_runs()
        summ: dict[str, Any] = {"run": None, "status": "no open run"}
        if runs:
            # Report the most recent run, so an agent that just sealed one sees its summary.
            rc = ctx.run_context(runs[0]["run_id"])
            summ = summary(rc) if rc.run.header.status != "open" else summ
        text = "done: " + ", ".join(f"{a}={b}" for a, b in summ.items() if b is not None)
        return Result({"run": summ.get("run"), "steps": [], "done": True, "summary": summ, "nodes": [],
                       "start": Command.of(cmd).model_dump() if cmd else None},
                      text + (f"\nstart a new run: {Command.of(cmd).shell}" if cmd else ""),
                      next=(cmd, "start a run") if cmd else None)
    rc = ctx.run_context()
    tok = ctx.token(rc, create=False)
    steps = compute_steps(rc, token=tok, full=bool(ns.full))
    if rc.run.header.status == "open":
        lease.heartbeat(store.db, rc.run.id, tok)
    data = envelope(rc, steps)
    text = steps_summary(steps) if not data["done"] else \
        "done: " + ", ".join(f"{a}={b}" for a, b in (data["summary"] or {}).items() if b is not None)
    return Result(data, text, next=steps_next(steps))


@command("run start", "Create a run of a graph, pinning its clock. Repeating it returns the same run.",
         mutating=True,
         args=[Arg("graph", "Graph name from docket.yaml."),
               Arg("--date", "Run date: today, tomorrow, a weekday name or YYYY-MM-DD."),
               Arg("--now", "Give the clock this ISO instant (tests, replays). The run then plans from it only; without "
                           "it a plan places nothing before the moment it is built."),
               Arg("--sandbox", "Bind every destination to its sandbox twin for the whole run.", action="store_true"),
               Arg("--new", "Create a new run even if one exists for this graph and date.", action="store_true")],
         effects=["creates runs/<run-id>/ with run.json and a session token", "adds a runs row", "takes the run lease"],
         examples=["docket run start morning --json", "docket run start morning --date tomorrow --sandbox --json"])
def cmd_run_start(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import closest
    from docket.engine.clock import pin
    from docket.engine.next import compute_steps, envelope
    from docket.store.runs import create_run, find_existing, load_run, new_run_id
    ws = ctx.workspace()
    if ns.graph not in ws.config.graphs:
        raise InputError("input.unknown_graph", f"No graph {ns.graph!r}.", hint="docket config explain graphs",
                         details={"suggestion": closest(ns.graph, list(ws.config.graphs)),
                                  "graphs": sorted(ws.config.graphs)})
    clock = pin(ws.config.profile.timezone, now=ns.now, on_date=ns.date)
    store = ctx.store()
    existing = None if ns.new else find_existing(store, ns.graph, clock.date)
    if existing is not None and bool(existing["sandbox"]) == bool(ns.sandbox):
        run = load_run(store, existing["run_id"])
        ctx.run_id = run.id
        rc = ctx.run_context(run.id)
        steps = compute_steps(rc, token=ctx.token(rc, create=False))
        return Result({**envelope(rc, steps), "created": False, "status": run.header.status,
                       "clock": run.header.clock.model_dump()},
                      f"run {run.id} already exists ({run.header.status})", next=steps_next(steps))
    run_id = new_run_id(store, clock.date, ns.graph)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_create": run_id, "clock": clock.model_dump()}, f"would create run {run_id}")
    with store.db:
        create_run(store, run_id=run_id, graph=ns.graph, clock=clock, sandbox=bool(ns.sandbox))
    rc = ctx.run_context(run_id)
    ctx.acquire(rc)
    steps = compute_steps(rc, token=ctx.token(rc, create=False))
    return Result({**envelope(rc, steps), "created": True, "status": "open", "clock": clock.model_dump(),
                   "sandbox": bool(ns.sandbox)},
                  f"started run {run_id}\n{steps_summary(steps)}", next=steps_next(steps))


@command("run status", "Show a run's header, its nodes' statuses, its lease and the steps that are ready. With "
                        "--graph, draw the run's graph with each node marked done, ready now, waiting or blocked.",
         run=True,
         args=[Arg("id", "Run id (default: --run, then the most recent unsealed run).", nargs="?"),
               Arg("--graph", "Draw the graph with the run's state, as docket graph show --run does.",
                   action="store_true"),
               Arg("--ascii", "With --graph, draw with plain ASCII characters.", action="store_true"),
               Arg("--width", "With --graph, wrap the drawing into bands this many characters wide (default 80).",
                   metavar="N")],
         examples=["docket run status --json", "docket run status --graph", "docket run status 2026-10-16-daily --graph"])
def cmd_run_status(ctx: Any, ns: Any) -> Result:
    from docket.engine.next import compute_steps
    from docket.store import lease
    rc = ctx.run_context(ns.id)
    row = lease.get(rc.run.store.db, rc.run.id)
    steps = compute_steps(rc, token=ctx.token(rc, create=False))
    nodes = rc.node_report()
    data = {"header": rc.run.header.model_dump(), "dir": str(rc.run.dir), "nodes": nodes,
            "lease": dict(row) if row else None, "steps": [s.model_dump() for s in steps]}
    text = "\n".join([f"run {rc.run.id} ({rc.run.header.status})"] +
                     [f"  {n['node']:<20} {n['state']}" + (f" ({n['reason']})" if n["reason"] else "") for n in nodes])
    if ns.graph:
        from docket.engine.drawing import layout, run_states
        lay = layout(rc.graph)
        states = run_states(nodes, lay)
        for n in nodes:
            p = lay.nodes[n["node"]]
            n.update(column=p.column, row=p.row, mark=states[n["node"]]["mark"])
        text = drawing(rc.graph_name(), rc.graph, lay, states, ns, rc)
    return Result(data, text, next=steps_next(steps))


@command("run list", "List runs in this workspace, newest first.", examples=["docket run list --json"])
def cmd_run_list(ctx: Any, ns: Any) -> Result:
    ctx.workspace_root()
    rows = [dict(r) for r in ctx.store().list_runs()]
    return Result({"runs": rows}, "\n".join(f"{r['run_id']:<32} {r['status']}" for r in rows) or "no runs")


@command("run diff", "Compare two runs of this workspace: counts side by side, items kept in one run only with the "
                      "reason from the trace, items whose score, recommended decision or answer differs, and "
                      "operations that differ by destination. Items are matched by topic. Works on sealed, open "
                      "and sandbox runs.",
         args=[Arg("a", "The first run id (A)."), Arg("b", "The second run id (B)."),
               Arg("--limit", "Rows shown per group (default 20); each group gives its total.", metavar="N")],
         examples=["docket run diff 2026-10-15-daily 2026-10-16-daily",
                   "docket run diff 2026-10-16-daily 2026-10-16-daily-2 --limit 5",
                   "docket run diff 2026-10-15-daily 2026-10-16-daily --json"])
def cmd_run_diff(ctx: Any, ns: Any) -> Result:
    from docket.engine.context import RunContext
    from docket.engine.run_diff import compare, snapshot, text
    from docket.store.runs import load_run
    try:
        limit = int(ns.limit) if ns.limit is not None else 20
    except ValueError:
        limit = -1
    if limit < 1:
        raise InputError("input.bad_argument", f"--limit must be a whole number of at least 1, not {ns.limit!r}.",
                         hint="docket run diff <run-a> <run-b> --limit 20")
    store = ctx.store()
    ws = ctx.workspace()
    runs = [load_run(store, ns.a), load_run(store, ns.b)]
    snaps, head = [], {}
    for key, run in zip(("a", "b"), runs):
        rc = None
        if run.header.status == "open" and run.header.graph in ws.config.graphs:
            rc = RunContext(ws, run, ctx.env)  # an open run as it stands now, answers since its artifacts too
        snaps.append(snapshot(run, rc))
        head[key] = {"run": run.id, "graph": run.header.graph, "status": run.header.status,
                     "sandbox": run.header.sandbox}
    diff = compare(snaps[0], snaps[1], limit=limit)
    return Result({**head, **diff}, text(head, diff))


def _guard_mutable(rc: Any) -> None:
    if rc.run.header.status == "sealed":
        raise StateError("run.sealed", f"Run {rc.run.id} is sealed.", hint="docket run start <graph> --new")
    if rc.run.header.status == "abandoned":
        raise StateError("run.abandoned", f"Run {rc.run.id} was abandoned.", hint="docket run start <graph> --new")


@command("run continue", "Finish a run on what it wrote after upstream changed past the apply boundary.",
         run=True, mutating=True,
         effects=["records in run.json that the operator saw the post-apply change"],
         examples=["docket run continue --json"])
def cmd_run_continue(ctx: Any, ns: Any) -> Result:
    from docket.apply.boundary import drift_digest, unacknowledged
    from docket.engine.next import compute_steps
    rc = ctx.run_context()
    rows = unacknowledged(rc)
    if not rows:
        steps = compute_steps(rc, token=ctx.token(rc, create=False))
        return Result({"run": rc.run.id, "acknowledged": [], "changed": False,
                       "steps": [s.model_dump() for s in steps]},
                      f"nothing to continue past\n{steps_summary(steps)}", next=steps_next(steps))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_acknowledge": rows}, f"would continue past {len(rows)} change(s)")
    ctx.acquire(rc)
    rc.run.header.drift_ack = drift_digest(rows)
    rc.run.save_header()
    rc.invalidate()
    steps = compute_steps(rc, token=ctx.token(rc, create=False))
    return Result({"run": rc.run.id, "acknowledged": rows, "changed": True, "steps": [s.model_dump() for s in steps]},
                  f"continuing on what was written\n{steps_summary(steps)}", next=steps_next(steps))


@command("run steal", "Take the run lease from another session.", mutating=True,
         args=[Arg("id", "Run id.")], effects=["replaces the lease owner"],
         examples=["docket run steal 2026-10-16-daily --json"])
def cmd_run_steal(ctx: Any, ns: Any) -> Result:
    from docket.store import lease
    rc = ctx.run_context(ns.id)
    row = lease.get(rc.run.store.db, rc.run.id)
    if ctx.dry_run:
        return Result({"dry_run": True, "current_owner": row["owner"] if row else None}, "would steal the lease")
    rep = ctx.acquire(rc, force=True)
    return Result({"run": rc.run.id, "owner": rep["owner"], "previous_owner": row["owner"] if row else None},
                  f"lease on {rc.run.id} now held by {rep['owner']}",
                  next=(rc.argv("next"), "continue the run"))


@command("run abandon", "Abandon a run. It advances no cursors; the next run re-reads.", mutating=True,
         args=[Arg("id", "Run id.")], effects=["marks the run abandoned"],
         examples=["docket run abandon 2026-10-16-daily --json"])
def cmd_run_abandon(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context(ns.id)
    if rc.run.header.status == "abandoned":
        return Result({"run": rc.run.id, "status": "abandoned", "changed": False}, "already abandoned")
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_abandon": rc.run.id}, f"would abandon {rc.run.id}")
    ctx.acquire(rc)
    with rc.run.store.db:
        rc.run.set_status("abandoned")
        rc.run.store.db.execute("DELETE FROM leases WHERE run_id=?", (rc.run.id,))
    return Result({"run": rc.run.id, "status": "abandoned", "changed": True}, f"abandoned {rc.run.id}")


def _width(ns: Any) -> int:
    raw = getattr(ns, "width", None)
    if raw is None:
        return 80
    try:
        w = int(raw)
    except ValueError:
        w = 0
    if w < 40:
        raise InputError("input.bad_argument", f"--width must be a whole number of at least 40, not {raw!r}.",
                         hint="docket graph show <graph> --width 100")
    return w


def drawing(name: str, graph: dict[str, Any], lay: Any, states: Any, ns: Any, rc: Any = None) -> str:
    """The text drawing of a graph, with a run's state when ``rc`` is given, and the nodes'
    descriptions under it."""
    from docket.config.models import ordered
    from docket.engine.drawing import draw
    if rc is not None:
        title = (f"run {rc.run.id} of graph {name} ({rc.run.header.status}"
                 + (", sandbox" if rc.run.header.sandbox else "") + ")")
    else:
        title = f"graph {name}: {len(graph)} node(s) in {lay.columns - 1} column(s), then the seal"
    text = draw(lay, width=_width(ns), ascii=bool(getattr(ns, "ascii", False)), states=states, title=title)
    notes = [f"  {n}: {graph[n].description}" for n in ordered(graph) if graph[n].description]
    return text + ("\n\ndescriptions:\n" + "\n".join(notes) if notes else "")


@command("graph show", "Draw a graph: its nodes left to right by how much must happen before each, what each does "
                       "and who does it (docket computes, an agent judges, the operator is asked). Solid lines hand "
                       "on data; dotted lines only order. With --run, mark each node of that run done, ready now, "
                       "waiting or blocked.",
         args=[Arg("graph", "Graph name from docket.yaml (default with --run: the run's graph).", nargs="?"),
               Arg("--run", "Overlay this run's state; with no id, the most recent unsealed run. Its graph must be "
                            "the graph named.", nargs="?", const="", metavar="RUN"),
               Arg("--list", "Print the flat list: each node, its verb, who does it and what it waits for.",
                   action="store_true"),
               Arg("--format", "text (the drawing, default) or mermaid (a Mermaid flowchart).",
                   choices=["text", "mermaid"], default="text"),
               Arg("--ascii", "Draw with plain ASCII characters.", action="store_true"),
               Arg("--width", "Wrap the drawing into bands this many characters wide (default 80).",
                   metavar="N")],
         examples=["docket graph show morning", "docket graph show morning --run", "docket graph show morning --ascii",
                   "docket graph show morning --format mermaid", "docket graph show morning --list",
                   "docket graph show morning --json"])
def cmd_graph_show(ctx: Any, ns: Any) -> Result:
    from docket.config.models import ordered
    from docket.engine.drawing import SEAL, WHO, flat, layout, mermaid, run_states
    from docket.engine.nodes import KINDS
    ws = ctx.workspace()
    rc = None
    name = ns.graph
    if ns.run is not None:
        rc = ctx.run_context(ns.run or None)
        if name is not None and name != rc.graph_name():
            raise InputError("run.graph_mismatch", f"Run {rc.run.id} is a run of graph {rc.graph_name()!r}, "
                             f"not {name!r}.", hint=f"docket graph show {rc.graph_name()} --run {rc.run.id}",
                             details={"run": rc.run.id, "run_graph": rc.graph_name(), "graph": name})
        name = rc.graph_name()
    if name is None:
        raise InputError("input.bad_argument", "Name a graph, or a run with --run.", hint="docket config explain graphs",
                         details={"graphs": sorted(ws.config.graphs)})
    if name not in ws.config.graphs:
        raise InputError("input.unknown_graph", f"No graph {name!r}.", hint="docket config explain graphs",
                         details={"graphs": sorted(ws.config.graphs)})
    graph = ws.config.graphs[name]
    lay = layout(graph)
    states = run_states(rc.node_report(), lay) if rc is not None else None
    rows = []
    for node in ordered(graph):
        n, p = graph[node], lay.nodes[node]
        row: dict[str, Any] = {
            "node": p.name, "verb": n.verb, "what": getattr(n, n.verb) if n.verb not in ("merge", "plan") else None,
            "by": WHO[KINDS[n.verb].how], "from": n.inputs, "after": [a for a in n.after if a not in n.inputs],
            "description": n.description, "column": p.column, "row": p.row}
        if n.verb == "ask" and n.questions.covered is not None:
            row["meetings"] = n.questions.covered.meetings
        if states is not None:
            row.update(states[p.name])
        rows.append(row)
    seal = {"column": lay.nodes[SEAL].column, "row": lay.nodes[SEAL].row, "after": lay.nodes[SEAL].needs}
    data: dict[str, Any] = {"graph": name, "nodes": rows, "seal": seal}
    if rc is not None:
        seal.update(states[SEAL])
        data.update(run=rc.run.id, status=rc.run.header.status)
    if ns.list:
        text = flat(rows, states)
    elif ns.format == "mermaid":
        text = mermaid(lay, states)
    else:
        text = drawing(name, graph, lay, states, ns, rc)
    return Result(data, text)
