"""Run commands and the loop entry point: ``next``, ``run start|status|list|resume|steal|abandon``."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError, StateError
from docket.cli.registry import Arg, command
from docket.engine.steps import Command, DoneStep


def _step_next(step: Any) -> tuple[list[str], str] | None:
    if getattr(step, "kind", None) == "run":
        return step.command.argv, step.why
    return None


def step_summary(step: Any) -> str:
    k = step.kind
    if k == "run":
        return f"run: {step.command.shell}\nwhy: {step.why}"
    if k == "dispatch":
        return f"dispatch {len(step.tasks)} task(s) ({step.stage}); concurrency {step.concurrency}"
    if k == "ask":
        lines = [f"ask {len(step.questions)} question(s) ({step.stage}):"]
        for q in step.questions[:20]:
            opts = ", ".join(f"{o.position}={o.id}" for o in q.options)
            lines.append(f"  {q.id}: {q.prompt} [{opts}]")
        return "\n".join(lines)
    if k == "wait":
        return f"wait ({step.reason}): {step.instructions}"
    return "done: " + ", ".join(f"{a}={b}" for a, b in step.summary.items() if b is not None)


@command("next", "Return the single next step for the run: run, dispatch, ask, wait or done.", run=True,
         effects=["renews the lease heartbeat when this session holds it; nothing else"],
         examples=["docket next --json"])
def cmd_next(ctx: Any, ns: Any) -> Result:
    from docket.engine.next import compute_step
    from docket.store import lease
    store = ctx.store()
    if not getattr(ns, "run", None) and store.latest_open() is None:
        ws = ctx.workspace()
        wfs = sorted(ws.config.workflows)
        cmd = ["docket", "run", "start", wfs[0], "--workspace", str(ws.root), "--json"] if wfs else None
        runs = store.list_runs()
        summ: dict[str, Any] = {"run": None, "status": "no open run"}
        if runs:
            # Report the most recent run, so an agent that just closed one sees its summary.
            rc = ctx.run_context(runs[0]["run_id"])
            summ = compute_step(rc).summary if rc.run.header.status != "open" else summ
        step = DoneStep(summary=summ, command=Command.of(cmd) if cmd else None)
        return Result({"run": summ.get("run"), "step": step.model_dump(), "stages": []},
                      step_summary(step) + (f"\nstart a new run: {step.command.shell}" if cmd else ""),
                      next=(cmd, "start a run") if cmd else None)
    rc = ctx.run_context()
    tok = ctx.token(rc, create=False)
    step = compute_step(rc, token=tok)
    if rc.run.header.status == "open":
        lease.heartbeat(store.db, rc.run.id, tok)
    return Result({"run": rc.run.id, "step": step.model_dump(), "stages": rc.stage_report()},
                  step_summary(step), next=_step_next(step))


@command("run start", "Create a run of a workflow, pinning its clock. Repeating it returns the same run.",
         mutating=True,
         args=[Arg("workflow", "Workflow name from docket.yaml."),
               Arg("--date", "Run date: today, tomorrow, a weekday name or YYYY-MM-DD."),
               Arg("--now", "Pin the clock to this ISO instant (rehearsal and tests)."),
               Arg("--sandbox", "Bind every destination to its sandbox twin for the whole run.", action="store_true"),
               Arg("--new", "Create a new run even if one exists for this workflow and date.", action="store_true")],
         effects=["creates runs/<run-id>/ with run.json and a session token", "adds a runs row", "takes the run lease"],
         examples=["docket run start daily --json", "docket run start daily --date tomorrow --sandbox --json"])
def cmd_run_start(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import closest
    from docket.engine.clock import pin
    from docket.engine.next import compute_step
    from docket.store.runs import create_run, find_existing, load_run, new_run_id
    ws = ctx.workspace()
    if ns.workflow not in ws.config.workflows:
        raise InputError("input.unknown_workflow", f"No workflow {ns.workflow!r}.", hint="docket config explain workflows",
                         details={"suggestion": closest(ns.workflow, list(ws.config.workflows)),
                                  "workflows": sorted(ws.config.workflows)})
    clock = pin(ws.config.profile.timezone, now=ns.now, on_date=ns.date)
    store = ctx.store()
    existing = None if ns.new else find_existing(store, ns.workflow, clock.date)
    if existing is not None and bool(existing["sandbox"]) == bool(ns.sandbox):
        run = load_run(store, existing["run_id"])
        ctx.run_id = run.id
        rc = ctx.run_context(run.id)
        step = compute_step(rc, token=ctx.token(rc, create=False))
        return Result({"run": run.id, "created": False, "status": run.header.status, "clock": run.header.clock.model_dump(),
                       "step": step.model_dump()},
                      f"run {run.id} already exists ({run.header.status})", next=_step_next(step))
    run_id = new_run_id(store, clock.date, ns.workflow)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_create": run_id, "clock": clock.model_dump()}, f"would create run {run_id}")
    with store.db:
        create_run(store, run_id=run_id, workflow=ns.workflow, clock=clock, sandbox=bool(ns.sandbox))
    rc = ctx.run_context(run_id)
    ctx.acquire(rc)
    step = compute_step(rc, token=ctx.token(rc, create=False))
    return Result({"run": run_id, "created": True, "status": "open", "clock": clock.model_dump(),
                   "sandbox": bool(ns.sandbox), "step": step.model_dump()},
                  f"started run {run_id}", next=_step_next(step))


@command("run status", "Show a run's header, stage statuses, lease and next step.", run=True,
         args=[Arg("id", "Run id (default: --run, then the most recent unsealed run).", nargs="?")],
         examples=["docket run status --json"])
def cmd_run_status(ctx: Any, ns: Any) -> Result:
    from docket.engine.next import compute_step
    from docket.store import lease
    rc = ctx.run_context(ns.id)
    row = lease.get(rc.run.store.db, rc.run.id)
    step = compute_step(rc, token=ctx.token(rc, create=False))
    stages = rc.stage_report()
    data = {"header": rc.run.header.model_dump(), "dir": str(rc.run.dir), "stages": stages,
            "lease": dict(row) if row else None, "step": step.model_dump()}
    text = "\n".join([f"run {rc.run.id} ({rc.run.header.status})"] +
                     [f"  {s['stage']:<8} {s['state']}" + (f" ({s['reason']})" if s["reason"] else "") for s in stages])
    return Result(data, text, next=_step_next(step))


@command("run list", "List runs in this workspace, newest first.", examples=["docket run list --json"])
def cmd_run_list(ctx: Any, ns: Any) -> Result:
    ctx.workspace_root()
    rows = [dict(r) for r in ctx.store().list_runs()]
    return Result({"runs": rows}, "\n".join(f"{r['run_id']:<32} {r['status']}" for r in rows) or "no runs")


def _guard_mutable(rc: Any) -> None:
    if rc.run.header.status == "sealed":
        raise StateError("run.sealed", f"Run {rc.run.id} is sealed.", hint="docket run start <workflow> --new")
    if rc.run.header.status == "abandoned":
        raise StateError("run.abandoned", f"Run {rc.run.id} was abandoned.", hint="docket run start <workflow> --new")


@command("run resume", "Resume a run paused before a stage (the workflow's pause_before).", run=True, mutating=True,
         effects=["records the resume in run.json"], examples=["docket run resume --json"])
def cmd_run_resume(ctx: Any, ns: Any) -> Result:
    from docket.engine.next import compute_step
    rc = ctx.run_context()
    pb = rc.workflow.pause_before
    if rc.run.header.status != "open" or not pb or pb in rc.run.header.resumed:
        step = compute_step(rc, token=ctx.token(rc, create=False))
        return Result({"run": rc.run.id, "resumed": None, "changed": False, "step": step.model_dump()},
                      "nothing to resume", next=_step_next(step))
    if ctx.dry_run:
        return Result({"dry_run": True, "would_resume": pb}, f"would resume before {pb}")
    ctx.acquire(rc)
    rc.run.header.resumed.append(pb)
    rc.run.save_header()
    rc.invalidate()
    step = compute_step(rc, token=ctx.token(rc, create=False))
    return Result({"run": rc.run.id, "resumed": pb, "changed": True, "step": step.model_dump()},
                  f"resumed before {pb}", next=_step_next(step))


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


@command("run rehearse", "Drive a copy of a run to its end against sandbox twins and recorded records; report "
                         "every stage outcome, operation, refusal and unexpected shape.",
         args=[Arg("--run", "Run id (default: the most recent open run, else the most recent run).", metavar="RUN"),
               Arg("--answers", "Where answers come from: recorded (then recommended, then first option), "
                                "recommended, or file:<path> (an ask answer --file document).", default="recorded")],
         effects=["writes runs/<run-id>/rehearse-<n>/ only: a copied state root, synthetic results and report.json",
                  "nothing in the live run, database, workspace or destinations changes"],
         examples=["docket run rehearse --json", "docket run rehearse --run 2026-10-16-daily --answers recommended --json"])
def cmd_run_rehearse(ctx: Any, ns: Any) -> Result:
    from docket.engine.rehearse import Rehearsal, latest_run
    from docket.store.paths import state_root
    ws = ctx.workspace()
    live_root = state_root(ws.root, ctx.env)
    run_id = ns.run or latest_run(live_root)
    if not run_id or not (live_root / "runs" / run_id / "run.json").is_file():
        raise StateError("rehearse.no_run", f"No run {run_id!r} to rehearse." if run_id else "No run to rehearse.",
                         hint=ctx.start_hint())
    ctx.run_id = run_id
    rep = Rehearsal(ws=ws, live_root=live_root, run_id=run_id, env=ctx.env, answers=ns.answers, cwd=ctx.cwd).run()
    lines = [f"rehearsal {rep['rehearsal']} of {run_id}: {'ok' if rep['ok'] else 'NOT ok'} in {rep['elapsed_s']}s "
             f"(reached {rep['reached'].get('kind') if rep['reached'] else '?'})"]
    lines += [f"  {s['stage']:<8} {s['state']}" + (f" ({s['reason']})" if s["reason"] else "") for s in rep["stages"]]
    lines.append(f"operations: {len(rep['operations'])}; refusals: {len(rep['refusals'])}; "
                 f"unexpected shapes: {len(rep['unexpected'])}")
    lines += [f"  refused: {r['code']}: {r['message']}" for r in rep["refusals"][:20]]
    lines.append(f"report: {rep['report']}")
    return Result(rep, "\n".join(lines))
