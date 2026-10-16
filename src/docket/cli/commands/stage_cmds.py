"""Commands that advance stages (TD §5.3): ``collect``, ``merge``, ``plan build``, ``apply``,
``close``, plus ``task issue|list|show|submit`` and ``ask list|show|answer``.

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
from docket.cli.errors import InputError, IntegrityError, StateError
from docket.cli.registry import Arg, command
from docket.config.digest import canonical_json, pretty_json


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
    if ctx.dry_run:
        return Result({"dry_run": True, "stage": stage, "state": ev.state, "digest": ev.digest,
                       "would": impl.expect}, f"would run {stage}: {impl.expect}")
    ctx.acquire(rc)
    rc.materialize()
    up = rc.upstream(stage)
    data = impl.execute(rc, ev, up)
    impl.finish(rc, ev, data)
    if stage == "close":
        with rc.run.store.db:
            rc.run.set_status("sealed")
            rc.run.store.db.execute("DELETE FROM leases WHERE run_id=?", (rc.run.id,))
    rc.invalidate()
    issued = _auto_issue(rc)
    step = _compute(ctx, rc)
    return Result({"stage": stage, "status": "current", "changed": True, "digest": ev.digest,
                   "result": _brief(data), "issued_tasks": issued, "step": step.model_dump()},
                  f"{stage} done\n{step_summary(step)}", next=_step_next(step))


_STAGE_EFFECT = {
    "collect": ["reads configured sources", "writes blobs, gen-<n> record indexes and the collect artifact",
                "writes cursor proposals (applied at close)"],
    "merge": ["writes the merge artifact"],
    "plan": ["writes plan/<digest>/plan.json and diff.md and the plan artifact"],
    "apply": ["writes to destinations (sandbox twins in a --sandbox run)", "writes receipts"],
    "close": ["advances cursors (not in a --sandbox run)", "writes dispositions and journal.md", "seals the run"],
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
        pending = [p["task_id"] for p in ev.extra["packs"] if p["task_id"] not in ev.extra["issued"]]
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


@command("task submit", "Submit a task result (a file path, or - for stdin).", run=True, mutating=True,
         args=[Arg("task_id", "Task id."), Arg("result", "Path to the JSON result, or - to read stdin.")],
         effects=["validates the result", "writes tasks/<task-id>/result.json when accepted"],
         examples=["docket task submit t_0123456789ab result.json --json"])
def cmd_task_submit(ctx: Any, ns: Any) -> Result:
    from docket.tasks.packs import pack_ids
    from docket.tasks.submit import validate_result
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
    if ns.result == "-":
        raw = (ctx.stdin.read() if ctx.stdin is not None else "")
    else:
        rp = Path(ns.result)
        if not rp.is_absolute():
            rp = ctx.cwd / rp
        try:
            raw = rp.read_text(encoding="utf-8")
        except OSError as e:
            raise InputError("input.bad_argument", f"Cannot read {rp}: {e.strerror}") from None
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise InputError("input.bad_json", f"The result is not JSON: {e}",
                         hint=f"docket schema {'assess-result' if pack['kind'] == 'assess' else 'review-result'}") from None
    result = validate_result(pack["kind"], payload, pack_ids(pack))
    body = result.model_dump(mode="json")
    tdir = rc.run.paths.task(ns.task_id)
    rpath = tdir / "result.json"
    if rpath.is_file() and canonical_json(json.loads(rpath.read_text(encoding="utf-8"))) == canonical_json(body):
        step = _compute(ctx, rc)
        return Result({"task_id": ns.task_id, "accepted": True, "changed": False, "step": step.model_dump()},
                      f"{ns.task_id} already accepted with this result\n{step_summary(step)}", next=_step_next(step))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "task_id": ns.task_id, "valid": True, "rows": len(body["items"])},
                      f"{ns.task_id}: result is valid (not written)")
    ctx.acquire(rc)
    from docket.store.paths import write_atomic
    write_atomic(rpath, pretty_json(body))
    rc.invalidate()
    rc.materialize()
    issued = _auto_issue(rc)
    step = _compute(ctx, rc)
    return Result({"task_id": ns.task_id, "accepted": True, "changed": True, "rows": len(body["items"]),
                   "issued_tasks": issued, "step": step.model_dump()},
                  f"accepted {ns.task_id}\n{step_summary(step)}", next=_step_next(step))


# --- questions ---------------------------------------------------------------


def _questions(rc: Any) -> list[tuple[Any, dict[str, Any], dict[str, Any] | None]]:
    """(stage eval, question, current answer) for every question reachable now."""
    out = []
    for ev in rc.evaluate():
        if ev.name == "decide" and "questions" in ev.extra:
            for q in ev.extra["questions"]:
                out.append((ev, q, ev.extra["answers"].get(q["id"])))
        if ev.name == "approve" and "question" in ev.extra:
            out.append((ev, ev.extra["question"], ev.extra["answer"]))
    return out


@command("ask list", "List the questions reachable now and their answers.", run=True,
         examples=["docket ask list --json"])
def cmd_ask_list(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    rows = [{"id": q["id"], "kind": q["kind"], "stage": ev.name, "prompt": q["prompt"],
             "options": [{"position": i, "id": o["id"], "label": o["label"]} for i, o in enumerate(q["options"], 1)],
             "answer": a} for ev, q, a in _questions(rc)]
    return Result({"questions": rows},
                  "\n".join(f"{r['id']}: {r['prompt']} {'[answered]' if r['answer'] else ''}" for r in rows) or "no questions")


@command("ask show", "Show one question with its options and answer.", run=True, args=[Arg("question_id", "Question id.")],
         examples=["docket ask show q_0123456789abcdef --json"])
def cmd_ask_show(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    for ev, q, a in _questions(rc):
        if q["id"] == ns.question_id:
            opts = "\n".join(f"  {i}. {o['id']}: {o['label']}" for i, o in enumerate(q["options"], 1))
            return Result({"question": q, "answer": a, "stage": ev.name}, f"{q['id']}: {q['prompt']}\n{opts}")
    raise InputError("input.unknown_question", f"No question {ns.question_id!r}.", hint="docket ask list")


@command("ask answer", "Record an answer. Options resolve by id, label, leading words or position.",
         run=True, mutating=True,
         args=[Arg("question_id", "Question id (q_..., or approve)."),
               Arg("option", "Option id, label, leading words of the label, or 1-based position."),
               Arg("--due", "Due date: today, tomorrow, a weekday name (next occurrence after the run date), YYYY-MM-DD."),
               Arg("--est", "Estimate: 45, 45m, 1h, 1.5h, 1h30m."),
               Arg("--when", "Window: a day, a day with times (thu 13:00-17:00), 'this week', or the title of "
                             "a calendar event to schedule over."),
               Arg("--title", "Task title override."),
               Arg("--note", "Free-text note kept with the answer."),
               Arg("--digest", "Plan digest; required to approve.")],
         effects=["writes answers.json"],
         examples=["docket ask answer q_9a1c task --due monday --est 60 --json",
                   "docket ask answer approve yes --digest <plan-digest> --json"])
def cmd_ask_answer(ctx: Any, ns: Any) -> Result:
    from docket.decide.options import resolve_option
    from docket.engine.clock import run_date
    from docket.plan.windows import normalize_when
    from docket.text.dates import normalize_date
    from docket.text.durations import format_duration, parse_duration
    rc = ctx.run_context()
    match = next(((ev, q, a) for ev, q, a in _questions(rc) if q["id"] == ns.question_id), None)
    if match is None:
        blocker = rc.first_pending()
        if blocker is not None and blocker.name not in ("decide", "approve") and \
                any(s in rc.workflow.stages for s in ("decide", "approve")):
            raise _not_ready(rc, ctx, blocker, "ask answer")
        known = [q["id"] for _, q, _ in _questions(rc)]
        raise InputError("input.unknown_question", f"No question {ns.question_id!r}.", hint="docket ask list",
                         details={"known": known[:50]})
    ev, q, current = match
    if ev.reason == "paused":
        raise _not_ready(rc, ctx, ev, "ask answer")
    opt = resolve_option(ns.option, q["options"], question_id=q["id"])
    ref = run_date(rc.run.header.clock)
    overrides: dict[str, Any] = {}
    if ns.due:
        overrides["due"] = normalize_date(ns.due, ref)
    if ns.est:
        overrides["est"] = parse_duration(ns.est)
    if ns.when:
        overrides["when"] = normalize_when(ns.when, ref)
    if ns.title:
        overrides["title"] = ns.title
    if ns.note:
        overrides["note"] = ns.note
    answer: dict[str, Any] = {"option": opt["id"], "action": opt.get("action"), "overrides": overrides,
                              "input": ns.option}
    if q["kind"] == "approve":
        if not ns.digest:
            raise InputError("input.missing_digest", "Approving a plan requires --digest with the plan digest.",
                             hint=" ".join(rc.argv("ask", "answer", "approve", opt["id"], "--digest", q["digest"])))
        if ns.digest != q["digest"]:
            raise IntegrityError("approve.digest_mismatch",
                                 "The digest does not match the current plan; the plan changed since it was shown.",
                                 hint=" ".join(rc.argv("next")),
                                 details={"expected": q["digest"], "received": ns.digest, "diff_path": q.get("diff_path")},
                                 next_command=rc.argv("next"))
        answer["digest"] = ns.digest
    echo = dict(answer)
    if "est" in overrides:
        echo["est_normalized"] = format_duration(overrides["est"])
    preview = None
    if q["kind"] == "finding":
        from docket.engine.stages import STAGE_IMPLS
        item = next((it for it in (rc.upstream("decide").output or {}).get("items", []) if it["id"] == q["item_id"]), None)
        if item is not None:
            preview = STAGE_IMPLS["plan"].build_op(rc, q["id"], item, opt, overrides) if "plan" in rc.workflow.stages else None
    stored = {k: current.get(k) for k in ("option", "action", "overrides", "digest")} if current else None
    if stored is not None and stored == {k: answer.get(k) for k in ("option", "action", "overrides", "digest")}:
        step = _compute(ctx, rc)
        return Result({"question_id": q["id"], "answer": echo, "preview": preview, "changed": False,
                       "step": step.model_dump()},
                      f"{q['id']}: unchanged ({opt['id']})\n{step_summary(step)}", next=_step_next(step))
    _guard_mutable(rc)
    if rc.run.header.applied_plan:
        raise StateError("stage.post_apply_change", "Answers cannot change after apply wrote receipts.",
                         hint="docket run status")
    if ctx.dry_run:
        return Result({"dry_run": True, "question_id": q["id"], "answer": echo, "preview": preview},
                      f"would record {q['id']} = {opt['id']}")
    ctx.acquire(rc)
    answers = rc.run.answers()
    answers[q["id"]] = answer
    rc.run.write_answers(answers)
    rc.invalidate()
    rc.materialize()
    step = _compute(ctx, rc)
    return Result({"question_id": q["id"], "answer": echo, "preview": preview, "changed": True,
                   "step": step.model_dump()},
                  f"{q['id']} = {opt['id']}" + (f" -> {preview['line']}" if preview else "") +
                  f"\n{step_summary(step)}", next=_step_next(step))
