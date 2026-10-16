"""Question commands (TD §10.3): ``ask list``, ``ask show``, ``ask answer``, ``ask undo`` and
``ask resolve``.

``ask answer <question-id> <option> [overrides]`` records one answer;
``ask answer --file answers.json`` records a batch, all or nothing. Every
answer is echoed in normalized form (the canonical command included) with the
action preview its destination renders, and, when the answer names a day, how
full that day already is (FR-DEC-08).

A batch file is a list of rows, ``{"answers": [rows]}``, or an object keyed by
question id whose values are an option or a row (``docket schema answers-file``).
A row names its question as ``question`` (or ``question_id``, or ``id``)::

    {"question": "q_...", "option": "task", "due": "monday", "est": "60m", "when": "...",
     "title": "...", "action": "...", "links": ["..."], "area": "...", "forum": "...",
     "merge_with": "q_...", "note": "..."}
"""
from __future__ import annotations

import json
import shlex
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from docket.cli.commands.run_cmds import _guard_mutable, _step_next, brief_text, step_brief, step_summary
from docket.cli.commands.stage_cmds import _compute, _not_ready
from docket.cli.envelope import Result
from docket.cli.errors import DocketError, InputError, IntegrityError, StateError
from docket.cli.registry import Arg, command
from docket.decide.answers import ANSWERS_FILE_EXAMPLE, ANSWERS_FILE_FORMS

_KEYS = ("option", "action", "overrides", "digest", "destinations", "merged_into")


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


def _row(ev: Any, q: dict[str, Any], a: dict[str, Any] | None) -> dict[str, Any]:
    out = {"id": q["id"], "kind": q["kind"], "stage": ev.name, "prompt": q["prompt"],
           "options": [{"position": i, "id": o["id"], "label": o["label"], "action": o.get("action"),
                        "destination": o.get("destination"), "defaults": o.get("defaults") or {}}
                       for i, o in enumerate(q["options"], 1)],
           "answer": a}
    for k in ("evidence", "refused", "notes", "previous", "requires", "item_id", "block_key", "task_id"):
        if q.get(k):
            out[k] = q[k]
    return out


@command("ask list", "List the questions reachable now, their options and answers; a re-asked question shows its "
                     "previous answer.", run=True, examples=["docket ask list --json"])
def cmd_ask_list(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    rows = [_row(ev, q, a) for ev, q, a in _questions(rc)]
    text = "\n".join(f"{r['id']}: {r['prompt']}" + (" [answered]" if r["answer"] else "")
                     + (" [re-asked]" if r.get("previous") and not r["answer"] else "") for r in rows)
    return Result({"questions": rows}, text or "no questions")


@command("ask show", "Show one question with its evidence, options, refused options and answer.", run=True,
         args=[Arg("question_id", "Question id.")], examples=["docket ask show q_0123456789abcdef --json"])
def cmd_ask_show(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    for ev, q, a in _questions(rc):
        if q["id"] == ns.question_id:
            lines = [f"{q['id']}: {q['prompt']}"] + [f"  > {e}" for e in q.get("evidence") or []]
            lines += [f"  {i}. {o['id']}: {o['label']}" for i, o in enumerate(q["options"], 1)]
            lines += [f"  ! {n}" for n in q.get("notes") or []]
            if q.get("previous"):
                lines.append(f"  previous answer: {q['previous'].get('option')}")
            return Result({"question": q, "answer": a, "stage": ev.name, **_row(ev, q, a)}, "\n".join(lines))
    raise InputError("input.unknown_question", f"No question {ns.question_id!r}.", hint="docket ask list")


# --- answering --------------------------------------------------------------------------------


def _find(rc: Any, ctx: Any, qid: str) -> tuple[Any, dict[str, Any], dict[str, Any] | None]:
    match = next(((ev, q, a) for ev, q, a in _questions(rc) if q["id"] == qid), None)
    if match is None:
        if qid == "approve" and "approve" in rc.workflow.stages and not rc.run.header.applied_plan:
            from docket.apply.approve import check_unreachable
            check_unreachable(rc, dry_run=ctx.dry_run)  # a plan stale because a destination changed (TD §13.2)
        blocker = rc.first_pending()
        if blocker is not None and blocker.name not in ("decide", "approve") and \
                any(s in rc.workflow.stages for s in ("decide", "approve")):
            raise _not_ready(rc, ctx, blocker, "ask answer")
        known = [q["id"] for _, q, _ in _questions(rc)]
        raise InputError("input.unknown_question", f"No question {qid!r}.", hint="docket ask list",
                         details={"known": known[:50]})
    if match[0].reason == "paused":
        raise _not_ready(rc, ctx, match[0], "ask answer")
    return match


class Session:
    """One answering command: the question set, answers in memory, and lazily the calendar."""

    def __init__(self, rc: Any) -> None:
        self.rc = rc
        self.answers = rc.run.answers()
        self.original = dict(self.answers)
        self._cal: Any = None
        ev = rc.eval_or_none("decide")
        self.decide = ev
        self.qset = ev.extra.get("set") if ev is not None else None
        self.questions = {q["id"]: q for q in (ev.extra.get("questions") or [])} if ev is not None and \
            "questions" in ev.extra else {}
        self.previous = (ev.extra.get("previous") or {}) if ev is not None else {}

    def calendar(self) -> Any:
        if self._cal is None:
            from docket.plan.build import calendar_ctx
            from docket.shapes.unexpected import collecting
            with collecting():  # calendar shapes are recorded by plan build, not by every answer
                self._cal = calendar_ctx(self.rc)
        return self._cal

    def subject(self, qid: str) -> dict[str, Any] | None:
        return self.qset.subjects().get(qid) if self.qset is not None else None


def _day_info(s: Session, overrides: dict[str, Any]) -> dict[str, Any]:
    """The resolved window and the load of every day the answer names (FR-DEC-08, TD §12.4)."""
    from docket.plan.build import day_load, planning_active
    rc = s.rc
    if not planning_active(rc) or not (overrides.get("due") or overrides.get("when")):
        return {}
    out: dict[str, Any] = {}
    days: list[date] = []
    if overrides.get("due"):
        days.append(date.fromisoformat(overrides["due"]))
    cal = s.calendar()
    if overrides.get("when"):
        from docket.plan.windows import describe, resolve_window
        try:
            w = resolve_window(overrides["when"], cal.today, instances=cal.cal.instances, lo=cal.today,
                               hi=cal.today + timedelta(days=cal.policy.horizon_days), tz=cal.tz)
        except DocketError as e:  # plan build refuses it; the echo says so now
            out["window"] = {"when": overrides["when"], "resolved": None, "error": e.code, "message": e.message}
        else:
            if w is not None:
                win: dict[str, Any] = {"when": overrides["when"], "resolved": describe(w),
                                       "segments": [{"date": d.isoformat(), "start": f"{a // 60:02d}:{a % 60:02d}",
                                                     "end": f"{b // 60:02d}:{b % 60:02d}"} for d, a, b in w.segments]}
                if w.event_id:
                    ev = next((e for e, _ in cal.cal.instances if e.id == w.event_id), None)
                    win["event"] = {"id": w.event_id, "title": ev.title if ev else None}
                out["window"] = win
                days += [d for d, _, _ in w.segments[:7]]
    loads = []
    for d in dict.fromkeys(days):
        loads.append(day_load(cal, d))
    if loads:
        out["day_load"] = loads
    return out


def _preview(s: Session, q: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """The action preview of an answer: destination operations or the outcome it records."""
    from docket.decide import operations as dops
    rc = s.rc
    ov = answer.get("overrides") or {}
    out: dict[str, Any] = {"operations": [], "outcome": None}
    if q["kind"] == "finding":
        item = s.subject(q["id"])
        if item is None:
            return out
        if answer["action"] in dops.NON_DESTINATION:
            out["outcome"] = dops.outcome(q["id"], item, answer["action"], ov)
        elif answer["action"] == "calendar.place":
            from docket.plan.build import planning_active
            out["schedule"] = {"title": ov.get("title") or ov.get("action") or item.get("title"),
                               "est": ov.get("est") or (q.get("defaults") or {}).get("est"),
                               "when": ov.get("when") or "the scheduler's choice"} if planning_active(rc) else None
        elif "plan" in rc.workflow.stages:
            merged = [s.subject(m) for m in ov.get("merge_with") or []]
            opt = next(o for o in q["options"] if o["id"] == answer["option"])
            out["operations"] = dops.finding_ops(rc, q["id"], item, opt, ov, [m for m in merged if m])
            if merged:
                out["merged"] = {"questions": ov["merge_with"], "titles": [m.get("title") for m in merged if m]}
    elif q["kind"] in ("reconcile", "verify"):
        row = s.subject(q["id"])
        if row is not None:
            mod = __import__("docket.decide." + q["kind"], fromlist=["operations"])
            out["operations"], adj = mod.operations(rc, q["id"], row, answer)
            if adj:
                out["task_effect"] = adj
    out.update(_day_info(s, ov))
    return out


def _normalize(rc: Any, ctx: Any, s: Session, qid: str, option_text: str, raw: dict[str, Any],
               digest_arg: str | None) -> dict[str, Any]:
    from docket.decide.answers import check_required, command_args, normalize_overrides, resolve_merge_targets
    from docket.decide.options import resolve_option
    from docket.engine.clock import run_date
    from docket.text.durations import format_duration
    ev, q, current = _find(rc, ctx, qid)
    if option_text in (None, ""):
        raise InputError("input.bad_argument", f"Answer {qid} needs an option.", hint=f"docket ask show {qid}")
    opt = resolve_option(str(option_text), q["options"], question_id=q["id"])
    ov = normalize_overrides(rc, raw, run_date(rc.run.header.clock))
    if ov.get("merge_with"):
        ov["merge_with"] = resolve_merge_targets(ov["merge_with"], q["id"], s.questions, s.answers)
    base_argv = rc.argv("ask", "answer", q["id"], opt["id"])
    check_required(q, opt["id"], ov, base_argv)
    answer: dict[str, Any] = {"kind": q["kind"], "option": opt["id"], "action": opt.get("action"),
                              "destination": opt.get("destination"), "overrides": ov, "input": str(option_text)}
    if q.get("subject"):
        answer["subject"] = q["subject"]
    if q["kind"] == "approve":
        from docket.apply.approve import bind
        answer = bind(rc, q, opt["id"], digest_arg,
                      {"option": opt["id"], "action": opt.get("action"), "overrides": ov, "input": str(option_text)},
                      dry_run=ctx.dry_run)  # TD §13.1, §13.2
    echo = {k: v for k, v in answer.items() if k != "subject"}
    if "est" in ov:
        echo["est_normalized"] = format_duration(ov["est"])
    echo["normalized"] = shlex.join(["docket", "ask", "answer", q["id"], opt["id"], *command_args(ov)])
    preview = _preview(s, q, answer) if q["kind"] != "approve" else {}
    return {"ev": ev, "q": q, "current": current, "answer": answer, "echo": echo, "preview": preview}


def _core(answers: dict[str, Any]) -> dict[str, Any]:
    """Answers compared by what they decide: the operator's exact words for the option do not count."""
    return {k: ({kk: v.get(kk) for kk in _KEYS} if isinstance(v, dict) else v) for k, v in answers.items()}


def _record(s: Session, row: dict[str, Any]) -> None:
    from docket.decide.answers import with_answer
    q = row["q"]
    if q["kind"] == "approve":
        s.answers = {**s.answers, "approve": row["answer"]}
        return
    s.answers = with_answer(s.answers, q, row["answer"], s.questions, s.previous)


def _rows_from_file(ctx: Any, path: str) -> list[dict[str, Any]]:
    if path == "-":
        raw = ctx.stdin.read() if ctx.stdin is not None else ""
    else:
        p = Path(path)
        if not p.is_absolute():
            p = ctx.cwd / p
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as e:
            raise InputError("input.bad_argument", f"Cannot read {p}: {e.strerror}") from None
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise InputError("input.bad_json", f"The answers file is not JSON: {e}") from None
    if isinstance(data, dict) and isinstance(data.get("answers"), list):
        data = data["answers"]
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            rows.append({"question": k, **(v if isinstance(v, dict) else {"option": v})})
    elif isinstance(data, list):
        for v in data:
            if not isinstance(v, dict):
                raise InputError("input.bad_json", "Every answer row must be an object with question and option.",
                                 hint="docket schema answers-file --json")
            rows.append(dict(v))
    else:
        raise InputError("input.bad_json", "The answers file must be a list of rows or an object keyed by question.",
                         hint="docket schema answers-file --json")
    for i, r in enumerate(rows):
        qid = r.pop("question", None) or r.pop("question_id", None) or r.pop("id", None)
        r.pop("question_id", None)
        r.pop("id", None)
        if not qid:
            raise InputError("input.bad_json", f"Answer row {i + 1} names no question (use question or question_id).",
                             hint="docket schema answers-file --json", details={"row": i + 1, "keys": sorted(r)})
        r["question"] = str(qid)
    return rows


def _row_text(row: dict[str, Any]) -> str:
    q, a, p = row["q"], row["answer"], row["preview"]
    line = f"{q['id']} = {a['option']}"
    ops = p.get("operations") or []
    if ops:
        o = ops[0]
        line += " -> " + (o.get("line") or o.get("subject") or o.get("title") or o["verb"])
        if len(ops) > 1:
            line += f" (+{len(ops) - 1} operation(s))"
    elif p.get("outcome"):
        line += f" -> {p['outcome']['decision']}"
    if p.get("window"):
        line += f"\n  window: {p['window'].get('resolved') or p['window'].get('message')}"
    for d in p.get("day_load") or []:
        line += (f"\n  {d['date']}: {d['committed_minutes']}m committed, {d['free_minutes']}m free in "
                 f"{d['work_window']}, {len(d['owned_blocks'])} owned block(s)")
    return line + f"\n  normalized: {row['echo']['normalized']}"


def _write(ctx: Any, rc: Any, s: Session, rows: list[dict[str, Any]], *, batch: bool) -> Result:
    def body(r: dict[str, Any]) -> dict[str, Any]:
        ops = r["preview"].get("operations") or []
        return {"question_id": r["q"]["id"], "answer": r["echo"], "preview": ops[0] if ops else None,
                "action_preview": r["preview"]}
    if _core(s.answers) == _core(s.original):
        step = _compute(ctx, rc)
        brief = step_brief(rc, step)
        data = {"answers": [body(r) for r in rows]} if batch else body(rows[0])
        return Result({**data, "changed": False, "step": brief},
                      "\n".join(f"{r['q']['id']}: unchanged ({r['answer']['option']})" for r in rows)
                      + f"\n{brief_text(brief)}", next=_step_next(step))
    _guard_mutable(rc)
    if rc.run.header.applied_plan:
        raise StateError("stage.post_apply_change", "Answers cannot change after apply wrote receipts.",
                         hint="docket run status")
    if ctx.dry_run:
        data = {"answers": [body(r) for r in rows]} if batch else body(rows[0])
        return Result({"dry_run": True, **data}, "\n".join("would record " + _row_text(r) for r in rows))
    ctx.acquire(rc)
    rc.run.write_answers(s.answers)
    appr = s.answers.get("approve")
    if any(r["q"]["kind"] == "approve" for r in rows) and isinstance(appr, dict) and appr.get("option") == "yes":
        from docket.apply.approve import record
        record(rc, appr["digest"], appr)
    rc.invalidate()
    rc.materialize()
    step = _compute(ctx, rc)
    brief = step_brief(rc, step)
    data = {"answers": [body(r) for r in rows]} if batch else body(rows[0])
    return Result({**data, "changed": True, "step": brief},
                  "\n".join(_row_text(r) for r in rows) + f"\n{brief_text(brief)}", next=_step_next(step))


@command("ask answer", "Record an answer. Options resolve by id, label, leading words, position or destination; "
                       "overrides carry due, estimate, window, title, task text, links, area, forum, merge and note.",
         run=True, mutating=True,
         args=[Arg("question_id", "Question id (q_..., r_..., v_..., or approve).", nargs="?"),
               Arg("option", "Option id, label, leading words of the label, 1-based position or destination.",
                   nargs="?"),
               Arg("--due", "Due date: today, tomorrow, a weekday name (next occurrence after the run date), YYYY-MM-DD."),
               Arg("--est", "Estimate: 45, 45m, 1h, 1.5h, 1h30m, 60 minutes."),
               Arg("--when", "Window: a day, a day with times (thu 13:00-17:00), 'this week', or the title of "
                             "a calendar event to schedule over."),
               Arg("--title", "Title override (the task title, and the calendar block's title)."),
               Arg("--action", "Task text override: what the task line says to do."),
               Arg("--link", "A link to keep with the task (repeatable).", action="append"),
               Arg("--area", "Area override."),
               Arg("--forum", "Forum for an agenda item: a profile forum id or name."),
               Arg("--merge-with", "Another finding question this answer absorbs into one action (repeatable).",
                   action="append"),
               Arg("--note", "Free-text note kept with the answer."),
               Arg("--digest", "Plan digest; required to approve."),
               Arg("--file", "Answer several questions from a JSON file (or - for stdin), all or nothing.")],
         effects=["writes answers.json"],
         inputs={"--file": {"schema": "answers-file", "description": ANSWERS_FILE_FORMS,
                            "example": ANSWERS_FILE_EXAMPLE}},
         examples=["docket ask answer q_9a1c task --due monday --est 60 --merge-with q_77b2 --note 'after the review' --json",
                   "docket ask answer q_41d0 block --when 'thu 13:00-17:00' --title 'Draft pricing memo' --json",
                   "docket ask answer r_dk_t_5f20 partly --est 30 --json",
                   "docket ask answer --file answers.json --json",
                   "docket ask answer approve yes --digest <plan-digest> --json"])
def cmd_ask_answer(ctx: Any, ns: Any) -> Result:
    rc = ctx.run_context()
    s = Session(rc)
    if ns.file:
        if ns.question_id or ns.option:
            raise InputError("input.bad_argument", "Use either --file or a question and an option, not both.",
                             hint="docket help ask answer")
        rows = []
        for i, r in enumerate(_rows_from_file(ctx, ns.file), 1):
            qid, opt, dg = r.pop("question"), r.pop("option", None), r.pop("digest", None)
            try:
                row = _normalize(rc, ctx, s, qid, opt, r, dg)
            except DocketError as e:
                e.details = {**e.details, "row": i, "question": qid}
                e.message = f"row {i} ({qid}): {e.message}"
                raise
            _record(s, row)
            rows.append(row)
        if not rows:
            raise InputError("input.bad_json", "The answers file holds no answers.")
        return _write(ctx, rc, s, rows, batch=True)
    if not ns.question_id:
        raise InputError("input.bad_argument", "ask answer needs a question id and an option, or --file.",
                         hint="docket ask list")
    raw = {"due": ns.due, "est": ns.est, "when": ns.when, "title": ns.title, "action": ns.action,
           "links": ns.link or None, "area": ns.area, "forum": ns.forum, "merge_with": ns.merge_with or None,
           "note": ns.note}
    row = _normalize(rc, ctx, s, ns.question_id, ns.option, {k: v for k, v in raw.items() if v is not None},
                     ns.digest)
    _record(s, row)
    return _write(ctx, rc, s, [row], batch=False)


@command("ask undo", "Remove an answer so the question is asked again; undoing a merge frees what it absorbed.",
         run=True, mutating=True, args=[Arg("question_id", "Question id.")], effects=["writes answers.json"],
         examples=["docket ask undo q_0123456789abcdef --json"])
def cmd_ask_undo(ctx: Any, ns: Any) -> Result:
    from docket.decide.answers import without_answer
    rc = ctx.run_context()
    _find(rc, ctx, ns.question_id)
    stored = rc.run.answers()
    after, removed = without_answer(stored, ns.question_id)
    if not removed:
        step = _compute(ctx, rc)
        brief = step_brief(rc, step)
        return Result({"question_id": ns.question_id, "removed": [], "changed": False, "step": brief},
                      f"{ns.question_id} has no answer\n{brief_text(brief)}", next=_step_next(step))
    _guard_mutable(rc)
    if rc.run.header.applied_plan:
        raise StateError("stage.post_apply_change", "Answers cannot change after apply wrote receipts.",
                         hint="docket run status")
    if ctx.dry_run:
        return Result({"dry_run": True, "question_id": ns.question_id, "removed": removed},
                      f"would remove {', '.join(removed)}")
    ctx.acquire(rc)
    rc.run.write_answers(after)
    rc.invalidate()
    step = _compute(ctx, rc)
    brief = step_brief(rc, step)
    return Result({"question_id": ns.question_id, "removed": removed, "changed": True, "step": brief},
                  f"removed {', '.join(removed)}\n{brief_text(brief)}", next=_step_next(step))


@command("ask resolve", "Resolve the task references verification needs through their sources, before the burst.",
         run=True, mutating=True, effects=["reads the sources that claim each reference (resolve)",
                                           "writes decide/resolutions.json"],
         examples=["docket ask resolve --json"])
def cmd_ask_resolve(ctx: Any, ns: Any) -> Result:
    from docket.decide.verify import resolve_pending
    rc = ctx.run_context()
    ev = rc.eval_or_none("decide")
    if ev is None or ev.reason != "resolve":
        if ev is None:
            raise StateError("stage.not_in_workflow", f"Workflow {rc.workflow_name()!r} has no decide stage.",
                             hint=" ".join(rc.argv("next")))
        blocker = rc.first_pending()
        if blocker is not None and blocker.name != "decide" and ev.state != "current" and \
                rc.workflow.stages.index(blocker.name) < rc.workflow.stages.index("decide"):
            raise _not_ready(rc, ctx, blocker, "ask resolve")
        step = _compute(ctx, rc)
        return Result({"resolved": [], "changed": False, "step": step.model_dump()},
                      f"nothing to resolve\n{step_summary(step)}", next=_step_next(step))
    refs = list(ev.extra["set"].pending_refs)
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_resolve": refs}, f"would resolve {len(refs)} reference(s)")
    ctx.acquire(rc)
    cache = resolve_pending(rc, refs)
    rc.invalidate()
    step = _compute(ctx, rc)
    rows = [{"ref": r, **{k: cache[r].get(k) for k in ("source", "status", "state", "last_actor")}} for r in refs]
    return Result({"resolved": rows, "changed": True, "step": step.model_dump()},
                  f"resolved {len(refs)} reference(s)\n{step_summary(step)}", next=_step_next(step))
