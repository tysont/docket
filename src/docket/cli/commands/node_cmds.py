"""Commands that do a node's work (TD §5.3): ``do <node>`` and ``seal``, plus
``task issue|list|show|submit``. The ``ask`` commands are in ``ask_cmds``.

Each command checks that its node is ready (everything it needs is current), does the
work, and returns the steps that are ready next in ``data.steps``. A command for a node
that is already current is a no-op reporting ``current`` (AE-06).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docket.cli.commands.run_cmds import _guard_mutable, steps_next, steps_summary
from docket.cli.envelope import Result
from docket.cli.errors import IntegrityError, InputError, StateError
from docket.cli.registry import Arg, command
from docket.config.digest import canonical_json


def _steps(ctx: Any, rc: Any) -> list[Any]:
    from docket.engine.next import compute_steps
    return compute_steps(rc, token=ctx.token(rc, create=False))


def _dump(steps: list[Any]) -> list[dict[str, Any]]:
    return [s.model_dump() for s in steps]


def _auto_issue(rc: Any) -> list[str]:
    """Write the packs of every judge node that is ready and has packs not yet written."""
    from docket.engine.nodes import JudgeNode
    written: list[str] = []
    for ev in rc.ready():
        impl = rc.impls[ev.name]
        if isinstance(impl, JudgeNode) and ev.reason == "tasks":
            written += impl.issue(rc)
    if written:
        rc.invalidate()
    return written


def _not_ready(rc: Any, ctx: Any, ev: Any) -> StateError:
    if ev.reason == "post_apply_change":
        return StateError("node.post_apply_change",
                          f"{ev.name} changed after a node wrote receipts; the run cannot go backward.",
                          hint="docket run status")
    nxt = steps_next(_steps(ctx, rc))
    argv = nxt[0] if nxt else rc.argv("next")
    return StateError("node.not_ready", f"{ev.name} cannot run yet: {ev.reason or ev.state}.",
                      hint=" ".join(argv), next_command=argv,
                      details={"node": ev.name, "state": ev.state, "needs": ev.extra.get("needs") or []})


def _brief(data: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in data.items():
        if k in ("items", "context") and isinstance(v, list):
            out["item_count" if k == "items" else "context_count"] = len(v)
        else:
            out[k] = v
    return out


def advance(ctx: Any, node: str, *, again: bool = False) -> Result:
    """Do the work of computed node ``node``. ``again`` repeats a node that is held on a
    failure that may pass (a source that was unavailable, operations that failed)."""
    from docket.engine.nodes import SEAL
    rc = ctx.run_context()
    if node not in rc.impls:
        raise StateError("node.not_in_graph", f"Graph {rc.graph_name()!r} has no node {node!r}.",
                         hint=" ".join(rc.argv("next")), details={"nodes": list(rc.order)})
    impl = rc.impls[node]
    if impl.how != "computed":
        what = "task submit" if impl.how == "judged" else "ask answer"
        raise StateError("node.not_computed", f"{node} is not done by a command: it is {impl.how} (docket {what}).",
                         hint=" ".join(rc.argv("next")))
    ev = rc.eval(node)
    if ev.state == "current" and node == SEAL and rc.run.header.status == "open" and not ctx.dry_run:
        _seal(ctx, rc)  # the seal's artifact was written but the run not sealed (a crash between the two)
    if ev.state == "current":
        steps = _steps(ctx, rc)
        return Result({"node": node, "status": "current", "changed": False, "digest": ev.digest, "steps": _dump(steps)},
                      f"{node} is current; nothing to do\n{steps_summary(steps)}", next=steps_next(steps))
    _guard_mutable(rc)
    held = ev.state == "waiting" and ev.reason in ("source_unavailable", "apply_partial")
    if ev.state == "blocked" or (ev.state == "waiting" and not (held and again)):
        raise _not_ready(rc, ctx, ev)
    from docket.apply.boundary import unacknowledged
    drift = unacknowledged(rc)
    if drift:
        cont = rc.argv("run", "continue")
        raise StateError("node.post_apply_change",
                         "Upstream changed after a node wrote receipts: " +
                         "; ".join(f"{d['node']}: {d['why']}" for d in drift) +
                         ". Inspect it, then finish on what was written with the continue command.",
                         hint=" ".join(cont), next_command=cont, details={"drift": drift})
    if ctx.dry_run and node == SEAL:
        from docket.seal.run import preview_seal
        return Result({"dry_run": True, "node": node, "state": ev.state, "digest": ev.digest,
                       "would": impl.expect, "preview": preview_seal(rc)}, f"would run {node}: {impl.expect}")
    if ctx.dry_run:
        return Result({"dry_run": True, "node": node, "state": ev.state, "digest": ev.digest,
                       "would": impl.expect}, f"would run {node}: {impl.expect}")
    ctx.acquire(rc)
    rc.materialize()
    data = impl.execute(rc, ev, rc.upstream(node))
    impl.finish(rc, ev, data)
    if node == SEAL:
        _seal(ctx, rc)
    rc.invalidate()
    issued = _auto_issue(rc)
    steps = _steps(ctx, rc)
    return Result({"node": node, "status": "current", "changed": True, "digest": ev.digest,
                   "result": _brief(data), "issued_tasks": issued, "steps": _dump(steps)},
                  f"{node} done\n{steps_summary(steps)}", next=steps_next(steps))


def _seal(ctx: Any, rc: Any) -> None:
    with rc.run.store.db:
        rc.run.set_status("sealed")
        rc.run.store.db.execute("DELETE FROM leases WHERE run_id=?", (rc.run.id,))
    rc.invalidate()


@command("do", "Do the work of a node that Docket computes: read a source, run a command, merge, plan, apply "
         "or write.", run=True, mutating=True,
         args=[Arg("node", "Node name from the run's graph."),
               Arg("--again", "Repeat a node held on a failure that may pass: an unavailable source, failed "
                              "operations.", action="store_true")],
         effects=["does the node's work (reads its source, writes to destinations: sandbox twins in a --sandbox run)",
                  "writes the node's artifact"],
         examples=["docket do gather-mail --json", "docket do apply-writes --dry-run --json"])
def cmd_do(ctx: Any, ns: Any) -> Result:
    from docket.engine.nodes import SEAL
    if ns.node == SEAL:
        raise InputError("input.bad_argument", "The seal is not a node of the graph.", hint="docket seal")
    return advance(ctx, ns.node, again=bool(ns.again))


@command("seal", "Seal the run once every node is done: advance cursors, write the ledger and the journal.",
         run=True, mutating=True,
         effects=["advances cursors (not in a --sandbox run)", "writes dispositions and the ledger",
                  "writes journal.md and the journal destination when configured", "seals the run"],
         examples=["docket seal --json"])
def cmd_seal(ctx: Any, ns: Any) -> Result:
    from docket.engine.nodes import SEAL
    return advance(ctx, SEAL)


# --- tasks -------------------------------------------------------------------


def _judged(rc: Any) -> list[Any]:
    return [ev for ev in rc.evaluate() if "packs" in ev.extra]


def _task_rows(rc: Any) -> list[dict[str, Any]]:
    rows = []
    for ev in _judged(rc):
        for p in ev.extra["packs"]:
            tdir = rc.run.paths.task(p["task_id"])
            rows.append({
                "task_id": p["task_id"], "node": ev.name, "kind": p["kind"], "items": len(p["items"]),
                "status": "accepted" if p["task_id"] in ev.extra["accepted"]
                else ("issued" if p["task_id"] in ev.extra["issued"] else "not_issued"),
                "pack_path": str(tdir / "pack.json"), "prompt_path": str(tdir / "prompt.md"),
            })
    return rows


@command("task issue", "Write task packs and prompts for every judge node that is ready.", run=True, mutating=True,
         effects=["writes tasks/<task-id>/pack.json and prompt.md"], examples=["docket task issue --json"])
def cmd_task_issue(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    pending = [p["task_id"] for ev in rc.ready() if ev.reason == "tasks" for p in ev.extra["plan"].pending_issue()]
    if not pending:
        steps = _steps(ctx, rc)
        return Result({"issued": [], "changed": False, "steps": _dump(steps)},
                      f"no tasks to issue\n{steps_summary(steps)}", next=steps_next(steps))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_issue": pending}, f"would issue {len(pending)} task(s)")
    ctx.acquire(rc)
    rc.materialize()
    written = _auto_issue(rc)
    steps = _steps(ctx, rc)
    return Result({"issued": written, "changed": True, "steps": _dump(steps)},
                  f"issued {len(written)} task(s)\n{steps_summary(steps)}", next=steps_next(steps))


@command("task list", "List the run's agent tasks and their status.", run=True, examples=["docket task list --json"])
def cmd_task_list(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    rows = _task_rows(rc)
    return Result({"tasks": rows}, "\n".join(f"{r['task_id']} {r['node']:<16} {r['status']}" for r in rows) or "no tasks")


@command("task show", "Show one task's paths and status.", run=True, args=[Arg("task_id", "Task id.")],
         examples=["docket task show t_0123456789ab --json"])
def cmd_task_show(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    for r in _task_rows(rc):
        if r["task_id"] == ns.task_id:
            return Result({"task": r}, f"{r['task_id']} ({r['node']}, {r['status']})\npack: {r['pack_path']}\n"
                                       f"prompt: {r['prompt_path']}")
    raise InputError("input.unknown_task", f"No current task {ns.task_id!r}.", hint="docket task list")


def _check_issued_pack(rc: Any, pack: dict[str, Any]) -> None:
    """The pack the agent read must be the pack the current inputs produce (TD §14.1: a digest
    mismatch on an artifact a command consumes refuses). The task id is its input digest, so a
    difference means the file was edited after it was issued."""
    from docket.config.digest import digest
    from docket.store.paths import read_json
    pp = rc.run.paths.task(pack["task_id"]) / "pack.json"
    if not pp.is_file():
        return
    try:
        disk = read_json(pp)
    except ValueError:
        disk = None
    if isinstance(disk, dict):
        disk.pop("node", None)
    if not isinstance(disk, dict) or digest(disk) != digest(pack):
        raise IntegrityError(
            "integrity.stale_artifact",
            f"{pp} differs from the pack the current inputs produce (digest {pack.get('input_digest', '')[:12]}); "
            f"a result built from it cannot be accepted.",
            hint=f"Delete {pp}, run {' '.join(rc.argv('task', 'issue'))}, and redo the task from the new pack",
            details={"task_id": pack["task_id"], "pack_path": str(pp), "input_digest": pack.get("input_digest")})


@command("task submit", "Submit a task result (a file path, or - for stdin).", run=True, mutating=True,
         args=[Arg("task_id", "Task id."), Arg("result", "Path to the JSON result, or - to read stdin.")],
         effects=["validates the result", "writes tasks/<task-id>/result.json when accepted"],
         examples=["docket task submit t_0123456789ab result.json --json"])
def cmd_task_submit(ctx: Any, ns: Any) -> Result:
    from docket.engine.steps import Command
    from docket.tasks import index
    from docket.tasks.runner import submit_argv, validate_submission
    rc = ctx.run_context()
    found = None
    for ev in _judged(rc):
        for p in ev.extra["packs"]:
            if p["task_id"] == ns.task_id:
                found = (ev, p)
    if found is None:
        blocked = [ev for ev in rc.evaluate() if ev.state == "blocked"]
        known = [r["task_id"] for r in _task_rows(rc)]
        raise InputError("input.unknown_task", f"No current task {ns.task_id!r}.",
                         hint="docket next", details={"known": known[:50], "blocked_nodes": [b.name for b in blocked]})
    ev, pack = found
    plan = ev.extra["plan"]
    rerun = Command.of(submit_argv(rc, ns.task_id)).shell
    _check_issued_pack(rc, pack)
    if ns.result == "-":
        raw = (ctx.stdin.read() if ctx.stdin is not None else "")
    else:
        rp = Path(ns.result)
        if not rp.is_absolute():
            rp = ctx.cwd / rp
        try:
            raw = rp.read_text(encoding="utf-8")
        except OSError as e:
            raise InputError("input.bad_argument", f"Cannot read {rp}: {e.strerror}", hint=rerun) from None
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise InputError("input.bad_json", f"The result is not JSON: {e}",
                         hint=f"docket schema {plan.kind.output_schema}") from None
    body = validate_submission(rc, plan, pack, payload).body
    current = plan.results.get(ns.task_id)
    if current is not None and canonical_json(current) == canonical_json(body):
        steps = _steps(ctx, rc)
        return Result({"task_id": ns.task_id, "accepted": True, "changed": False, "steps": _dump(steps)},
                      f"{ns.task_id} already accepted with this result\n{steps_summary(steps)}", next=steps_next(steps))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "task_id": ns.task_id, "valid": True, "rows": len(body["items"])},
                      f"{ns.task_id}: result is valid (not written)")
    ctx.acquire(rc)
    index.record(rc, pack, body)
    rc.invalidate()
    rc.materialize()
    issued = _auto_issue(rc)
    steps = _steps(ctx, rc)
    return Result({"task_id": ns.task_id, "accepted": True, "changed": True, "rows": len(body["items"]),
                   "issued_tasks": issued, "steps": _dump(steps)},
                  f"accepted {ns.task_id}\n{steps_summary(steps)}", next=steps_next(steps))


# --- questions ---------------------------------------------------------------
# ``ask list|show|answer|undo|resolve`` live in ``docket.cli.commands.ask_cmds`` (M5).
