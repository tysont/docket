"""Commands that advance stages (TD §5.3): ``collect``, ``merge``, ``plan build``, ``apply``,
``close``, plus ``task issue|list|show|submit``. The ``ask`` commands are in ``ask_cmds``.

Each command checks that its stage is next (or stale), does the work, and returns
the following step in ``data.step``. A command for a stage that is already
current is a no-op reporting ``current`` (AE-06).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docket.cli.commands.run_cmds import _guard_mutable, _step_next, step_summary
from docket.cli.envelope import Result
from docket.cli.errors import IntegrityError, InputError, StateError
from docket.cli.registry import Arg, command
from docket.config.digest import canonical_json


def _compute(ctx: Any, rc: Any) -> Any:
    from docket.engine.next import compute_step
    return compute_step(rc, token=ctx.token(rc, create=False))


def _auto_issue(rc: Any) -> list[str]:
    """When the next stage is delegated and its packs are not written yet, write them now."""
    from docket.engine.stages import STAGE_IMPLS, DelegatedStage
    ev = rc.first_pending()
    if ev is None or ev.reason == "paused" or ev.state == "blocked":
        return []
    impl = STAGE_IMPLS[ev.name]
    if isinstance(impl, DelegatedStage):
        written = impl.issue(rc)
        if written:
            rc.invalidate()
        return written
    return []


def _not_ready(rc: Any, ctx: Any, blocker: Any, target: str) -> StateError:
    step = _compute(ctx, rc)
    if blocker.reason == "post_apply_change":
        return StateError("stage.post_apply_change",
                          f"{blocker.name} changed after apply wrote receipts; the run cannot go backward.",
                          hint="docket run status")
    if blocker.reason == "paused":
        resume = rc.argv("run", "resume")
        return StateError("run.paused", f"The workflow pauses before {blocker.name}.", hint=" ".join(resume),
                          next_command=resume, details={"stage": blocker.name})
    nxt = step.command.argv if getattr(step, "kind", "") == "run" else rc.argv("next")
    return StateError("stage.not_ready", f"{target} cannot run yet: {blocker.name} is {blocker.state}.",
                      hint=" ".join(nxt), next_command=nxt,
                      details={"blocking_stage": blocker.name, "state": blocker.state, "step_kind": step.kind})


def _brief(data: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in data.items():
        if k in ("items", "context") and isinstance(v, list):
            out["item_count" if k == "items" else "context_count"] = len(v)
        else:
            out[k] = v
    return out


def advance(ctx: Any, stage: str) -> Result:
    from docket.engine.stages import STAGE_IMPLS
    rc = ctx.run_context()
    if stage not in rc.workflow.stages:
        raise StateError("stage.not_in_workflow", f"Workflow {rc.workflow_name()!r} has no {stage} stage.",
                         hint=" ".join(rc.argv("next")), details={"stages": list(rc.workflow.stages)})
    impl = STAGE_IMPLS[stage]
    ev = rc.eval(stage)
    if ev.state == "current" and stage == "close" and rc.run.header.status == "open" and not ctx.dry_run:
        _seal(ctx, rc)  # the close artifact was written but the run not sealed (a crash between the two)
    if ev.state == "current":
        step = _compute(ctx, rc)
        return Result({"stage": stage, "status": "current", "changed": False, "digest": ev.digest,
                       "step": step.model_dump()},
                      f"{stage} is current; nothing to do\n{step_summary(step)}", next=_step_next(step))
    _guard_mutable(rc)
    blocker = rc.first_pending()
    if blocker is not None and blocker.name != stage:
        raise _not_ready(rc, ctx, blocker, stage)
    if ev.reason in ("paused", "post_apply_change"):
        raise _not_ready(rc, ctx, ev, stage)
    from docket.apply.boundary import unacknowledged
    drift = unacknowledged(rc)
    if drift:
        cont = rc.argv("run", "continue")
        raise StateError("stage.post_apply_change",
                         "Upstream changed after apply wrote receipts: " +
                         "; ".join(f"{d['stage']}: {d['why']}" for d in drift) +
                         ". Inspect it, then finish on the approved plan with the continue command.",
                         hint=" ".join(cont), next_command=cont, details={"drift": drift})
    if ctx.dry_run and stage == "close":
        from docket.close.run import preview_close
        return Result({"dry_run": True, "stage": stage, "state": ev.state, "digest": ev.digest,
                       "would": impl.expect, "preview": preview_close(rc)}, f"would run {stage}: {impl.expect}")
    if ctx.dry_run:
        return Result({"dry_run": True, "stage": stage, "state": ev.state, "digest": ev.digest,
                       "would": impl.expect}, f"would run {stage}: {impl.expect}")
    ctx.acquire(rc)
    rc.materialize()
    up = rc.upstream(stage)
    data = impl.execute(rc, ev, up)
    impl.finish(rc, ev, data)
    if stage == "close":
        _seal(ctx, rc)
    rc.invalidate()
    issued = _auto_issue(rc)
    step = _compute(ctx, rc)
    return Result({"stage": stage, "status": "current", "changed": True, "digest": ev.digest,
                   "result": _brief(data), "issued_tasks": issued, "step": step.model_dump()},
                  f"{stage} done\n{step_summary(step)}", next=_step_next(step))


def _seal(ctx: Any, rc: Any) -> None:
    with rc.run.store.db:
        rc.run.set_status("sealed")
        rc.run.store.db.execute("DELETE FROM leases WHERE run_id=?", (rc.run.id,))
    rc.invalidate()


_STAGE_EFFECT = {
    "collect": ["reads configured sources", "writes blobs, gen-<n> record indexes and the collect artifact",
                "writes cursor proposals (applied at close)"],
    "merge": ["writes the merge artifact"],
    "plan": ["writes plan/<digest>/plan.json and diff.md and the plan artifact"],
    "apply": ["writes to destinations (sandbox twins in a --sandbox run)", "writes receipts"],
    "close": ["advances cursors (not in a --sandbox run)", "writes dispositions and ledgers",
              "writes journal.md and the journal destination when configured", "runs after_close hooks",
              "seals the run"],
}


@command("collect", "Collect records from the workflow's sources; --refresh opens a new generation.", run=True,
         mutating=True, effects=_STAGE_EFFECT["collect"],
         args=[Arg("--refresh", "Open a new generation for volatile sources, reading from this run's coverage end.",
                   action="store_true"),
               Arg("--sources", "Comma-separated source ids to refresh (default: refresh_on_resume, else volatile).")],
         examples=["docket collect --json", "docket collect --refresh --sources chat,mail --json"])
def cmd_collect(ctx: Any, ns: Any) -> Result:
    from docket.cli.commands.source_cmds import collect_command
    return collect_command(ctx, ns)


@command("merge", "Merge assessed items.", run=True, mutating=True, effects=_STAGE_EFFECT["merge"],
         examples=["docket merge --json"])
def cmd_merge(ctx: Any, ns: Any) -> Result:
    return advance(ctx, "merge")


@command("plan build", "Build the plan from the answers.", run=True, mutating=True, effects=_STAGE_EFFECT["plan"],
         examples=["docket plan build --json"])
def cmd_plan_build(ctx: Any, ns: Any) -> Result:
    return advance(ctx, "plan")


@command("apply", "Apply the approved plan, writing one receipt per operation.", run=True, mutating=True,
         effects=_STAGE_EFFECT["apply"], examples=["docket apply --json", "docket apply --dry-run --json"])
def cmd_apply(ctx: Any, ns: Any) -> Result:
    return advance(ctx, "apply")


@command("close", "Close the run: advance cursors, write the journal, seal.", run=True, mutating=True,
         effects=_STAGE_EFFECT["close"], examples=["docket close --json"])
def cmd_close(ctx: Any, ns: Any) -> Result:
    return advance(ctx, "close")


# --- tasks -------------------------------------------------------------------


def _delegated(rc: Any) -> list[Any]:
    from docket.engine.stages import STAGE_IMPLS, DelegatedStage
    return [ev for ev in rc.evaluate() if isinstance(STAGE_IMPLS[ev.name], DelegatedStage) and "packs" in ev.extra]


def _task_rows(rc: Any) -> list[dict[str, Any]]:
    rows = []
    for ev in _delegated(rc):
        for p in ev.extra["packs"]:
            tdir = rc.run.paths.task(p["task_id"])
            rows.append({
                "task_id": p["task_id"], "stage": ev.name, "kind": p["kind"], "items": len(p["items"]),
                "status": "accepted" if p["task_id"] in ev.extra["accepted"]
                else ("issued" if p["task_id"] in ev.extra["issued"] else "not_issued"),
                "pack_path": str(tdir / "pack.json"), "prompt_path": str(tdir / "prompt.md"),
            })
    return rows


@command("task issue", "Write task packs and prompts for the next delegated stage.", run=True, mutating=True,
         effects=["writes tasks/<task-id>/pack.json and prompt.md"], examples=["docket task issue --json"])
def cmd_task_issue(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    from docket.engine.stages import STAGE_IMPLS, DelegatedStage
    ev = rc.first_pending()
    pending = []
    if ev is not None and isinstance(STAGE_IMPLS[ev.name], DelegatedStage) and ev.state == "waiting" \
            and ev.reason != "paused":
        pending = [p["task_id"] for p in ev.extra["plan"].pending_issue()]
    if not pending:
        step = _compute(ctx, rc)
        return Result({"issued": [], "changed": False, "step": step.model_dump()},
                      f"no tasks to issue\n{step_summary(step)}", next=_step_next(step))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_issue": pending}, f"would issue {len(pending)} task(s)")
    ctx.acquire(rc)
    rc.materialize()
    written = _auto_issue(rc)
    step = _compute(ctx, rc)
    return Result({"issued": written, "changed": True, "step": step.model_dump()},
                  f"issued {len(written)} task(s)\n{step_summary(step)}", next=_step_next(step))


@command("task list", "List the run's agent tasks and their status.", run=True, examples=["docket task list --json"])
def cmd_task_list(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    rows = _task_rows(rc)
    return Result({"tasks": rows}, "\n".join(f"{r['task_id']} {r['stage']:<7} {r['status']}" for r in rows) or "no tasks")


@command("task show", "Show one task's paths and status.", run=True, args=[Arg("task_id", "Task id.")],
         examples=["docket task show t_0123456789ab --json"])
def cmd_task_show(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    for r in _task_rows(rc):
        if r["task_id"] == ns.task_id:
            return Result({"task": r}, f"{r['task_id']} ({r['stage']}, {r['status']})\npack: {r['pack_path']}\n"
                                       f"prompt: {r['prompt_path']}")
    raise InputError("input.unknown_task", f"No current task {ns.task_id!r}.", hint="docket task list")


def _check_issued_pack(rc: Any, pack: dict[str, Any]) -> None:
    """The pack the agent read must be the pack the current inputs produce (TD §14.1: a stage digest
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
        disk.pop("issued_generation", None)
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
    for ev in _delegated(rc):
        for p in ev.extra["packs"]:
            if p["task_id"] == ns.task_id:
                found = (ev, p)
    if found is None:
        blocked = [ev for ev in rc.evaluate() if ev.state == "blocked"]
        known = [r["task_id"] for r in _task_rows(rc)]
        raise InputError("input.unknown_task", f"No current task {ns.task_id!r}.",
                         hint="docket next", details={"known": known[:50], "blocked_stages": [b.name for b in blocked]})
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
                         hint=f"docket schema {plan.kinds[pack['kind']].output_schema}") from None
    body = validate_submission(rc, plan, pack, payload).body
    current = plan.results.get(ns.task_id)
    if current is not None and canonical_json(current) == canonical_json(body):
        step = _compute(ctx, rc)
        return Result({"task_id": ns.task_id, "accepted": True, "changed": False, "step": step.model_dump()},
                      f"{ns.task_id} already accepted with this result\n{step_summary(step)}", next=_step_next(step))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "task_id": ns.task_id, "valid": True, "rows": len(body["items"])},
                      f"{ns.task_id}: result is valid (not written)")
    ctx.acquire(rc)
    index.record(rc, pack, body)
    rc.invalidate()
    rc.materialize()
    issued = _auto_issue(rc)
    step = _compute(ctx, rc)
    return Result({"task_id": ns.task_id, "accepted": True, "changed": True, "rows": len(body["items"]),
                   "issued_tasks": issued, "step": step.model_dump()},
                  f"accepted {ns.task_id}\n{step_summary(step)}", next=_step_next(step))


# --- questions ---------------------------------------------------------------
# ``ask list|show|answer|undo|resolve`` live in ``docket.cli.commands.ask_cmds`` (M5).
