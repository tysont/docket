"""The stage protocol and the M1 stage implementations (TD §4.4, §5.1).

Each stage names its schema version and the configuration sections it reads.
Its digest is a function of those sections, its upstream output digest, the
answers it reads and the run clock. An artifact whose file name equals the
fresh digest is current; any other is stale. Nothing else tracks staleness.

Kinds:

- computational (collect, merge, plan, apply, close): Docket does the work when
  the agent runs the stage's command.
- delegated (assess, review): complete when every task has an accepted result.
- interactive (decide, approve): complete when every question has an answer.

Delegated and interactive outputs are pure functions of results and answers;
they are computed in memory (``docket next`` writes nothing) and written as
artifacts by the next mutating command.

M1 implements every stage as a stub with the real interface; M2 replaces the
collect stub with real collection (``docket.sources``). The delegated stages issue one task per batch, decide asks
one question per kept item, plan lists task-line operations, apply appends
lines to a Markdown task store and close advances cursors and seals. M4
replaces the delegated stubs with real agent tasks (``docket.tasks``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel

from docket.cli.errors import ExternalError
from docket.config import contexts
from docket.config.defaults import DEFAULT_OPTION_SET, DEFAULT_SECTION, DEFAULT_TASK_LINE
from docket.config.digest import digest, pretty_json
from docket.engine.steps import (
    AskOption, AskQuestion, AskStep, Command, DispatchStep, RunStep, WaitStep,
)
from docket.shapes.unexpected import unexpected
from docket.store.paths import read_json, write_atomic
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

State = Literal["current", "stale", "pending", "waiting", "blocked"]


class InputRef(BaseModel):
    kind: Literal["artifact", "records", "answers"]
    stage: str
    digest: str


class StageStatus(BaseModel):
    """current | stale | pending (never built) | waiting(reason) | blocked(reason)."""
    stage: str
    kind: str
    state: State
    digest: str | None = None
    reason: str | None = None
    config_changed: bool | None = None


@dataclass
class StageEval:
    name: str
    kind: str
    state: State
    digest: str | None = None
    reason: str | None = None
    output: dict[str, Any] | None = None
    output_digest: str | None = None
    config_digest: str | None = None
    inputs: list[InputRef] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def status(self) -> StageStatus:
        return StageStatus(stage=self.name, kind=self.kind, state=self.state, digest=self.digest, reason=self.reason)


class Stage(Protocol):
    name: str
    schema_version: int
    config_sections: tuple[str, ...]

    def inputs(self, run: "RunContext") -> list[InputRef]: ...
    def status(self, run: "RunContext") -> StageStatus: ...
    def step(self, run: "RunContext") -> Any: ...


# ---------------------------------------------------------------------------


class BaseStage:
    name = ""
    kind = "computational"
    schema_version = 1
    config_sections: tuple[str, ...] = ()
    reads_time = False
    command: tuple[str, ...] = ()
    expect = ""

    # protocol --------------------------------------------------------------
    def inputs(self, run: "RunContext") -> list[InputRef]:
        return run.eval(self.name).inputs

    def status(self, run: "RunContext") -> StageStatus:
        return run.eval(self.name).status()

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        return RunStep(stage=self.name, command=Command.of(run.argv(*self.command)),
                       why=self._why(ev), expect=self.expect)

    # helpers ---------------------------------------------------------------
    def _why(self, ev: StageEval) -> str:
        if ev.state == "stale":
            return f"{self.name} is stale: its inputs or configuration changed since it was built"
        return f"{self.name} has not run yet"

    def stage_digest(self, run: "RunContext", inputs: list[InputRef], answers: str | None = None,
                     extra: Any = None) -> tuple[str, str]:
        cfg = run.partial_config(self.config_sections)
        body = {
            "stage": self.name, "schema": self.schema_version, "config": cfg,
            "inputs": [i.digest for i in inputs], "answers": answers,
            "clock": run.clock_view(with_time=self.reads_time), "extra": extra,
        }
        return digest(body), cfg

    def read_artifact(self, run: "RunContext", dg: str) -> dict[str, Any] | None:
        p = run.run.paths.artifact(self.name, dg)
        return read_json(p) if p.is_file() else None

    def has_any_artifact(self, run: "RunContext") -> bool:
        d = run.run.paths.artifacts(self.name)
        return d.is_dir() and any(p.suffix == ".json" for p in d.iterdir())

    def write_artifact(self, run: "RunContext", ev: StageEval) -> None:
        body = {
            "stage": self.name, "digest": ev.digest, "schema": self.schema_version,
            "config_digest": ev.config_digest, "inputs": [i.model_dump() for i in ev.inputs],
            "data": ev.output,
        }
        write_atomic(run.run.paths.artifact(self.name, ev.digest), pretty_json(body))
        write_atomic(run.run.paths.artifacts(self.name) / "latest", ev.digest + "\n")

    def latest_config_digest(self, run: "RunContext") -> str | None:
        p = run.run.paths.artifacts(self.name) / "latest"
        if not p.is_file():
            return None
        art = self.read_artifact(run, p.read_text().strip())
        return art.get("config_digest") if art else None

    def up_input(self, up: StageEval | None) -> list[InputRef]:
        if up is None or up.output_digest is None:
            return []
        return [InputRef(kind="artifact", stage=up.name, digest=up.output_digest)]

    def computational_eval(self, run: "RunContext", up: StageEval | None, *, extra: Any = None) -> StageEval:
        inputs = self.up_input(up)
        dg, cfg = self.stage_digest(run, inputs, extra=extra)
        art = self.read_artifact(run, dg)
        if art is not None:
            return StageEval(self.name, self.kind, "current", dg, None, art["data"], digest(art["data"]), cfg, inputs)
        state: State = "stale" if self.has_any_artifact(run) else "pending"
        return StageEval(self.name, self.kind, state, dg, "inputs changed" if state == "stale" else "not built",
                         None, None, cfg, inputs)

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        return self.computational_eval(run, up)

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        raise NotImplementedError

    def finish(self, run: "RunContext", ev: StageEval, data: dict[str, Any]) -> StageEval:
        ev.output, ev.output_digest, ev.state, ev.reason = data, digest(data), "current", None
        self.write_artifact(run, ev)
        return ev


# --- collect ---------------------------------------------------------------


class CollectStage(BaseStage):
    name = "collect"
    config_sections = ("sources", "transports", "workflow.sources")
    command = ("collect",)
    expect = "records per source under gen-<n>/records, cursor proposals and the collect artifact"

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        from docket.sources.generations import current_generation, unavailable_required
        ev = self.computational_eval(run, up, extra={"generation": current_generation(run)})
        if ev.state == "current":
            missing = unavailable_required(ev.output)
            if missing:
                ev.state, ev.reason = "waiting", "sources_unavailable"
                ev.extra["unavailable"] = missing
        return ev

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        if ev.reason == "sources_unavailable":
            missing = ev.extra["unavailable"]
            lines = [f"{sid}: {row.get('reason') or 'unavailable'}"
                     + (f" -> {row['instructions']}" if row.get("instructions") else "") for sid, row in missing.items()]
            return WaitStep(stage=self.name, reason="sources_unavailable",
                            instructions="Required source(s) could not be read. " + " | ".join(lines),
                            resume_when="the operator has fixed access to the named sources",
                            command=Command.of(run.argv("collect", "--refresh", "--sources", ",".join(sorted(missing)))))
        return super().step(run)

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        from docket.sources.stage import execute_collect
        return execute_collect(run)


# --- delegated stages --------------------------------------------------------


class DelegatedStage(BaseStage):
    """Task issuing, dispatch and submission live in ``docket.tasks.runner`` (TD §9)."""
    kind = "delegated"
    task_kind = ""

    def candidates(self, run: "RunContext", up: StageEval) -> list[dict[str, Any]]:
        raise NotImplementedError

    def combine(self, up: StageEval, plan: Any) -> dict[str, Any]:
        raise NotImplementedError

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        from docket.tasks.runner import plan_stage
        assert up is not None and up.output is not None
        inputs = self.up_input(up)
        dg, cfg = self.stage_digest(run, inputs)
        plan = plan_stage(run, self.name, self.candidates(run, up))
        extra = {"packs": plan.packs, "issued": plan.issued, "accepted": set(plan.results), "plan": plan}
        if plan.complete:
            out = self.combine(up, plan)
            return StageEval(self.name, self.kind, "current", dg, None, out, digest(out), cfg, inputs, extra)
        return StageEval(self.name, self.kind, "waiting", dg, "tasks", None, None, cfg, inputs, extra)

    def step(self, run: "RunContext") -> Any:
        from docket.tasks.runner import dispatch_tasks, dispatch_why
        ev = run.eval(self.name)
        plan = ev.extra["plan"]
        if plan.pending_issue():
            return RunStep(stage=self.name, command=Command.of(run.argv("task", "issue")),
                           why=f"{self.name} needs {len(plan.pending_issue())} task pack(s) written before dispatch",
                           expect="pack.json and prompt.md per task", timeout_s=60)
        tasks = dispatch_tasks(run, plan)
        kinds = sorted({t.kind for t in tasks})
        return DispatchStep(stage=self.name, tasks=tasks, concurrency=run.ws.config.tasks.concurrency,
                            why=dispatch_why(len(tasks), self.name, kinds))

    def issue(self, run: "RunContext") -> list[str]:
        from docket.tasks.runner import issue
        return issue(run, run.eval(self.name).extra["plan"])


class AssessStage(DelegatedStage):
    name = "assess"
    task_kind = "assess"
    config_sections = ("profile", "signals", "tasks")

    def candidates(self, run: "RunContext", up: StageEval) -> list[dict[str, Any]]:
        # M3: only items that need an agent (not ``assess: rules_only``, not dismissed); must-surface stay in.
        from docket.signals.stage import assess_selection
        return assess_selection(run, list(up.output["items"])).candidates

    def combine(self, up: StageEval, plan: Any) -> dict[str, Any]:
        from docket.tasks.runner import combine_assess
        return combine_assess(up.output["items"], plan)


class ReviewStage(DelegatedStage):
    name = "review"
    task_kind = "review"
    config_sections = ("profile", "tasks", "workflow.review")

    def candidates(self, run: "RunContext", up: StageEval) -> list[dict[str, Any]]:
        return [it for it in up.output["items"] if it.get("keep")]

    def combine(self, up: StageEval, plan: Any) -> dict[str, Any]:
        from docket.tasks.runner import combine_review
        return combine_review(up.output["items"], plan)


# --- merge -----------------------------------------------------------------


class MergeStage(BaseStage):
    """M3: rules, scores, merge by topic, suppression and coverage (``docket.signals``)."""
    name = "merge"
    config_sections = ("profile", "signals", "destinations")
    command = ("merge",)
    expect = "the merge artifact: topics with scores, statuses, traces and coverage notes"

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        from docket.signals.stage import merge_extra
        return self.computational_eval(run, up, extra=merge_extra(run))

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        from docket.signals.stage import execute_merge
        return execute_merge(run, up)


# --- decide ----------------------------------------------------------------


def item_context(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": it.get("id"), "source": it.get("source"), "unit": it.get("unit"), "title": it.get("title"),
        "text": it.get("text"), "from": it.get("from"), "at": it.get("at"), "url": it.get("url"),
        "score": it.get("score"), "fields": it.get("fields") or {}, "forum": {},
    }


class DecideStage(BaseStage):
    name = "decide"
    kind = "interactive"
    config_sections = ("decisions", "profile", "signals", "workflow.questions", "workflow.options")

    def options(self, run: "RunContext") -> list[dict[str, Any]]:
        sets = run.ws.config.decisions.option_sets
        name = run.workflow.options or DEFAULT_OPTION_SET
        chosen = sets.get(name, {}).get("finding")
        if chosen is None:
            unexpected("decide.options", f"option set {name} has no finding options",
                       reading="use the default option set", extend=f"decisions.option_sets.{name}.finding")
            chosen = sets[DEFAULT_OPTION_SET]["finding"]
        return [o.model_dump() for o in chosen]

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        assert up is not None and up.output is not None
        floor = int(((run.ws.config.signals.get("scoring") or {}).get("floor")) or 0)
        kept = [it for it in up.output["items"] if it.get("keep") and int(it.get("score") or 0) >= floor]
        kept.sort(key=lambda it: (-int(it.get("score") or 0), it["id"]))
        budget = run.workflow.questions.budget
        asked, not_asked = (kept[:budget], kept[budget:]) if budget is not None else (kept, [])
        prompt_tpl = templates.parse(run.ws.config.decisions.templates.get("finding_prompt", "{title}"))
        opts = self.options(run)
        label_tpls = {o["id"]: templates.parse(o["label"]) for o in opts}
        questions = []
        for it in asked:
            ctx = item_context(it)
            questions.append({
                "id": "q_" + it["id"], "kind": "finding", "item_id": it["id"],
                "prompt": templates.render(prompt_tpl, ctx),
                "options": [{"id": o["id"], "label": templates.render(label_tpls[o["id"]], ctx),
                             "action": o["action"], "destination": o.get("destination")} for o in opts],
            })
        answers = run.run.answers()
        relevant = {q["id"]: answers[q["id"]] for q in questions if q["id"] in answers}
        inputs = self.up_input(up)
        dg, cfg = self.stage_digest(run, inputs, answers=digest(relevant))
        extra = {"questions": questions, "answers": relevant}
        if len(relevant) == len(questions):
            out = {
                "questions": questions, "answers": relevant,
                "findings": [{"question_id": "q_" + it["id"], "item": it} for it in asked],
                "not_asked": [{"item_id": it["id"], "title": it.get("title", "")} for it in not_asked],
            }
            return StageEval(self.name, self.kind, "current", dg, None, out, digest(out), cfg, inputs, extra)
        return StageEval(self.name, self.kind, "waiting", dg, "answers", None, None, cfg, inputs, extra)

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        pending = [q for q in ev.extra["questions"] if q["id"] not in ev.extra["answers"]]
        return ask_step(run, self.name, pending,
                        why=f"{len(pending)} question(s) need the operator's answer; ask them, then record each "
                            f"answer with its option's command")


def ask_step(run: "RunContext", stage: str, questions: list[dict[str, Any]], why: str,
             extra_args: dict[str, list[str]] | None = None) -> AskStep:
    out = []
    for q in questions:
        opts = []
        for i, o in enumerate(q["options"], 1):
            argv = run.argv("ask", "answer", q["id"], o["id"], *((extra_args or {}).get(o["id"], [])))
            opts.append(AskOption(id=o["id"], label=o["label"], position=i, action=o.get("action"),
                                  answer=Command.of(argv)))
        details = {k: v for k, v in q.items() if k in ("item_id", "digest", "diff_path", "operations")}
        out.append(AskQuestion(id=q["id"], kind=q["kind"], prompt=q["prompt"], options=opts, details=details))
    pattern = " ".join(run.argv("ask", "answer", "<question-id>", "<option>"))
    return AskStep(stage=stage, questions=out, answer=pattern, why=why)


# --- plan ------------------------------------------------------------------


class PlanStage(BaseStage):
    name = "plan"
    config_sections = ("planning", "destinations", "decisions")
    reads_time = True
    command = ("plan", "build")
    expect = "a plan with its digest under plan/<digest>/ and a diff to show the operator"

    def target_destination(self, run: "RunContext", option: dict[str, Any]) -> str | None:
        if option.get("destination"):
            return option["destination"]
        for name in sorted(run.ws.config.destinations):
            if run.ws.config.destinations[name].adapter == "markdown_tasks":
                return name
        return None

    def build_op(self, run: "RunContext", qid: str, item: dict[str, Any], option: dict[str, Any],
                 overrides: dict[str, Any]) -> dict[str, Any] | None:
        """The operation an answer produces, or None when its action writes nothing.

        The destination's own ``preview`` renders it (grammar, quality rules,
        redaction, ownership); see ``docket.destinations.planning``.
        """
        from docket.destinations.planning import answer_operations
        ops = answer_operations(run, qid, item, option, overrides)
        return ops[0] if ops else None

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        from docket.plan.build import input_digests, planning_active
        extra = input_digests(run) if up is not None and up.output is not None and planning_active(run) else None
        return self.computational_eval(run, up, extra=extra)

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        from docket.plan.build import planning_active
        out = up.output
        qs = {q["id"]: q for q in out["questions"]}
        items = {f["question_id"]: f["item"] for f in out["findings"]}
        ops = []
        for qid in sorted(out["answers"]):
            ans = out["answers"][qid]
            opt = next((o for o in qs[qid]["options"] if o["id"] == ans["option"]), {})
            op = self.build_op(run, qid, items[qid], opt, ans.get("overrides") or {})
            if op is not None:
                ops.append(op)
        ops.sort(key=lambda o: (o["destination"], o["op_id"]))
        plan = {
            "inputs": {"decide": up.output_digest, "config_digest": ev.config_digest},
            "operations": ops, "schedule": [], "unschedulable": [], "notes": [],
        }
        if planning_active(run):  # M7: schedule open tasks and add calendar operations (TD §12)
            return self.execute_planning(run, plan, up)
        pd = digest(plan)
        pdir = run.run.paths.plan(pd)
        write_atomic(pdir / "plan.json", pretty_json(plan))
        diff = [f"# Plan {pd[:12]}", ""] + [f"- {o['destination']} ({o['path']} under {o['anchor']}): add `{o['line']}`"
                                           for o in ops]
        if not ops:
            diff.append("- No operations.")
        write_atomic(pdir / "diff.md", "\n".join(diff) + "\n")
        return {"plan_digest": pd, "operations": ops, "diff": pdir.relative_to(run.run.dir).as_posix() + "/diff.md"}

    def execute_planning(self, run: "RunContext", plan: dict[str, Any], up: StageEval) -> dict[str, Any]:
        from zoneinfo import ZoneInfo
        from docket.plan.build import extend_plan, render_diff
        built = extend_plan(run, plan, up.output, up.output_digest)
        pd = digest(plan)
        pdir = run.run.paths.plan(pd)
        write_atomic(pdir / "plan.json", pretty_json(plan))
        write_atomic(pdir / "diff.md", render_diff(plan, pd, ZoneInfo(run.run.header.clock.timezone)))
        return {"plan_digest": pd, "operations": plan["operations"],
                "diff": pdir.relative_to(run.run.dir).as_posix() + "/diff.md",
                "planning": {"blocks": len(plan["schedule"]), "unschedulable": len(plan["unschedulable"]),
                             "calendar_operations": len(built.ops)}}


# --- approve ---------------------------------------------------------------


class ApproveStage(BaseStage):
    name = "approve"
    kind = "interactive"
    config_sections = ()

    def question(self, run: "RunContext", up: StageEval) -> dict[str, Any]:
        pd = up.output["plan_digest"]
        n = len(up.output["operations"])
        diff = str(run.run.dir / up.output["diff"])
        return {
            "id": "approve", "kind": "approve", "digest": pd, "diff_path": diff, "operations": n,
            "prompt": f"Approve the plan ({n} operation(s), digest {pd[:12]})? Show the operator {diff} first.",
            "options": [{"id": "yes", "label": "Approve", "action": "approve"},
                        {"id": "no", "label": "Reject", "action": "reject"}],
        }

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        assert up is not None and up.output is not None
        q = self.question(run, up)
        ans = run.run.answers().get("approve")
        bound = ans if ans and ans.get("digest") == q["digest"] else None
        inputs = self.up_input(up)
        dg, cfg = self.stage_digest(run, inputs, answers=digest(bound))
        extra = {"question": q, "answer": bound}
        if bound and bound.get("option") == "yes":
            out = {"approved": True, "plan_digest": q["digest"], "operations": up.output["operations"]}
            return StageEval(self.name, self.kind, "current", dg, None, out, digest(out), cfg, inputs, extra)
        if bound:
            return StageEval(self.name, self.kind, "blocked", dg, "plan_rejected", None, None, cfg, inputs, extra)
        return StageEval(self.name, self.kind, "waiting", dg, "answers", None, None, cfg, inputs, extra)

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        q = ev.extra["question"]
        if ev.reason == "plan_rejected":
            return WaitStep(stage=self.name, reason="plan_rejected",
                            instructions="The operator rejected the plan. Change answers with docket ask answer, "
                                         "rebuild with docket plan build, or approve with the yes command.",
                            command=Command.of(run.argv("ask", "answer", "approve", "yes", "--digest", q["digest"])))
        return ask_step(run, self.name, [q],
                        why="Show the operator the plan diff and record approval bound to the plan digest",
                        extra_args={"yes": ["--digest", q["digest"]], "no": ["--digest", q["digest"]]})


# --- apply -----------------------------------------------------------------


class ApplyStage(BaseStage):
    name = "apply"
    config_sections = ("destinations",)
    command = ("apply",)
    expect = "one receipt per operation in receipts.jsonl"

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        ev = self.computational_eval(run, up, extra={"sandbox": run.run.header.sandbox})
        if ev.state == "current" and any(r.get("outcome") == "failed" for r in (ev.output or {}).get("receipts", [])):
            # an external failure can succeed on retry: hold the run at apply (TD §13.3). A refusal is final.
            ev.state, ev.reason = "waiting", "apply_partial"
        return ev

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        if ev.reason == "apply_partial":
            rows = [r for r in ev.output["receipts"] if r.get("outcome") in ("failed", "blocked")]
            return WaitStep(stage=self.name, reason="apply_partial",
                            instructions=f"{len(rows)} operation(s) failed or are blocked by a failed operation; "
                                         f"completed operations will not repeat. Fix the cause and run the retry "
                                         f"command.", command=Command.of(run.argv("apply")))
        return super().step(run)

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        from docket.apply import executor
        hdr = run.run.header
        if hdr.applied_plan != up.output["plan_digest"]:
            hdr.applied_plan = up.output["plan_digest"]
            run.run.save_header()
        rows = executor.execute(run, up.output["operations"])
        data = {"plan_digest": up.output["plan_digest"], "sandbox": hdr.sandbox, "receipts": rows}
        failed = [r for r in rows if r["outcome"] == "failed"]
        if failed:
            self.finish(run, ev, data)  # keep what completed; docket next now waits with apply_partial
            first = failed[0].get("error") or {}
            raise ExternalError("dest.apply_failed",
                                f"{len(failed)} operation(s) failed; first: {first.get('message', 'unknown error')}",
                                hint=" ".join(run.argv("apply")), retryable=True, next_command=run.argv("apply"),
                                details={"failed": failed, "receipts": len(rows)})
        return data


# --- close -----------------------------------------------------------------

JOURNAL_TEMPLATE = """# Docket journal: {run.id}

Workflow {run.workflow} for {run.date}{?run.sandbox} (sandbox run){/run.sandbox}.

## Sources
{#sources}- {id}: {outcome} ({records} records)
{/sources}
## Findings asked
{#findings}- {question_id}: {title}
{/findings}
## Answers
{#answers}- {question_id}: {option}
{/answers}
## Operations
{#operations}- {destination}: {line} [{outcome}]
{/operations}
## Not asked
{#not_asked}- {title}
{/not_asked}
## Unexpected shapes
{#unexpected}- {area} / {detail}: {reading} (extend: {extend})
{/unexpected}"""


class CloseStage(BaseStage):
    name = "close"
    config_sections = ("journal", "hooks", "sources")
    command = ("close",)
    expect = "cursors advanced, journal.md written and the run sealed"

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        r = run.run
        db = r.store.db
        from docket.sources.cursors import apply_proposals
        moved: dict[str, list[Any]] = {"advanced": [], "held": [], "regressions": []}
        with db:
            if not r.header.sandbox:
                moved = apply_proposals(db, r.id)
            dec = run.eval_or_none("decide")
            if dec is not None and dec.output:
                for qid, ans in sorted(dec.output["answers"].items()):
                    if ans.get("action") in ("dismiss", "watch", "done"):
                        item = qid[2:]
                        db.execute("INSERT OR REPLACE INTO dispositions (topic_id, decision, run_id, item, until, note) "
                                   "VALUES (?,?,?,?,?,?)", (item, ans["action"], r.id, item, None,
                                                           (ans.get("overrides") or {}).get("note")))
        journal = self.render_journal(run)
        write_atomic(r.paths.journal, journal)
        return {"cursors_advanced": moved["advanced"], "cursors_held": moved["held"],
                "cursor_regressions": moved["regressions"], "journal": "journal.md",
                "sealed": True, "sandbox": r.header.sandbox}

    def render_journal(self, run: "RunContext") -> str:
        r = run.run
        col = run.eval_or_none("collect")
        dec = run.eval_or_none("decide")
        app = run.eval_or_none("apply")
        sources = [{"id": k, **v} for k, v in sorted(((col.output or {}).get("sources") or {}).items())] if col else []
        findings, answers, not_asked = [], [], []
        if dec is not None and dec.output:
            titles = {f["question_id"]: f["item"].get("title", "") for f in dec.output["findings"]}
            findings = [{"question_id": q, "title": t} for q, t in titles.items()]
            answers = [{"question_id": q, "option": a["option"], "title": titles.get(q, "")}
                       for q, a in sorted(dec.output["answers"].items())]
            not_asked = dec.output.get("not_asked", [])
        ops = []
        if app is not None and app.output:
            outcomes = {x["op_id"]: x["outcome"] for x in app.output["receipts"]}
            appr = run.eval_or_none("approve")
            for o in (appr.output or {}).get("operations", []) if appr else []:
                ops.append({**o, "outcome": outcomes.get(o["op_id"], "pending")})
        unexpected_rows = []
        if r.paths.unexpected.is_file():
            seen = set()
            for ln in r.paths.unexpected.read_text(encoding="utf-8").splitlines():
                row = json.loads(ln)
                key = (row["area"], row["detail"])
                if key not in seen:
                    seen.add(key)
                    unexpected_rows.append(row)
        ctx = {
            "run": {"id": r.id, "date": r.header.clock.date, "workflow": r.header.workflow, "sandbox": r.header.sandbox},
            "sources": sources, "findings": findings, "answers": answers, "operations": ops,
            "not_asked": not_asked, "unexpected": unexpected_rows,
        }
        return templates.render(JOURNAL_TEMPLATE, ctx) + "\n"


STAGE_IMPLS: dict[str, BaseStage] = {
    s.name: s for s in (CollectStage(), AssessStage(), MergeStage(), ReviewStage(), DecideStage(),
                        PlanStage(), ApproveStage(), ApplyStage(), CloseStage())
}
