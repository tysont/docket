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
lines to a Markdown task store and close advances cursors and seals.
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
    AskOption, AskQuestion, AskStep, Command, DispatchStep, DispatchTask, RunStep, WaitStep,
)
from docket.shapes.unexpected import unexpected
from docket.store.paths import read_json, write_atomic
from docket.tasks.packs import build_packs, render_prompt
from docket.tasks.submit import RESULT_SCHEMAS
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
    kind = "delegated"
    task_kind = ""

    def candidates(self, up: StageEval) -> list[dict[str, Any]]:
        raise NotImplementedError

    def combine(self, up: StageEval, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError

    def evaluate(self, run: "RunContext", up: StageEval | None) -> StageEval:
        assert up is not None and up.output is not None
        inputs = self.up_input(up)
        dg, cfg = self.stage_digest(run, inputs)
        max_items = int(((run.ws.config.tasks.get(self.task_kind) or {}).get("max_items")) or 10)
        packs = build_packs(self.task_kind, self.candidates(up), max_items)
        results: dict[str, dict[str, Any]] = {}
        issued: set[str] = set()
        for p in packs:
            tdir = run.run.paths.task(p["task_id"])
            if (tdir / "pack.json").is_file():
                issued.add(p["task_id"])
            if (tdir / "result.json").is_file():
                results[p["task_id"]] = read_json(tdir / "result.json")
        extra = {"packs": packs, "issued": issued, "accepted": set(results)}
        if len(results) == len(packs):
            out = self.combine(up, results)
            return StageEval(self.name, self.kind, "current", dg, None, out, digest(out), cfg, inputs, extra)
        return StageEval(self.name, self.kind, "waiting", dg, "tasks", None, None, cfg, inputs, extra)

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        packs = ev.extra["packs"]
        if any(p["task_id"] not in ev.extra["issued"] for p in packs):
            return RunStep(stage=self.name, command=Command.of(run.argv("task", "issue")),
                           why=f"{self.name} needs {len(packs)} task pack(s) written before dispatch",
                           expect="pack.json and prompt.md per task", timeout_s=60)
        tasks = []
        for p in packs:
            if p["task_id"] in ev.extra["accepted"]:
                continue
            tdir = run.run.paths.task(p["task_id"])
            result_path = str(tdir / "submission.json")
            tasks.append(DispatchTask(
                task_id=p["task_id"], kind=self.task_kind, pack_path=str(tdir / "pack.json"),
                prompt_path=str(tdir / "prompt.md"), result_path=result_path,
                result_schema=RESULT_SCHEMAS[self.task_kind],
                submit=Command.of(run.argv("task", "submit", p["task_id"], result_path)),
            ))
        conc = int(run.ws.config.tasks.get("concurrency") or 6)
        return DispatchStep(stage=self.name, tasks=tasks, concurrency=conc,
                            why=f"{len(tasks)} independent {self.task_kind} task(s) need results; run them as "
                                f"subagents or inline, write each result to result_path and run its submit command")

    def issue(self, run: "RunContext") -> list[str]:
        ev = run.eval(self.name)
        written = []
        for p in ev.extra["packs"]:
            tdir = run.run.paths.task(p["task_id"])
            if (tdir / "pack.json").is_file():
                continue
            write_atomic(tdir / "pack.json", pretty_json(p))
            hint = (f"Write the JSON result to tasks/{p['task_id']}/submission.json in the run directory, then run:\n"
                    f"docket task submit --run {run.run.id} {p['task_id']} <that path>")
            write_atomic(tdir / "prompt.md", render_prompt(p, hint))
            written.append(p["task_id"])
        return written


class AssessStage(DelegatedStage):
    name = "assess"
    task_kind = "assess"
    config_sections = ("profile", "signals", "tasks")

    def candidates(self, up: StageEval) -> list[dict[str, Any]]:
        return list(up.output["items"])

    def combine(self, up: StageEval, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        rows = {row["id"]: row for res in results.values() for row in res["items"]}
        items = []
        for it in up.output["items"]:
            row = rows.get(it["id"], {})
            items.append({**it, "keep": bool(row.get("keep")), "score": int(row.get("score") or 0)})
        return {"items": items}


class ReviewStage(DelegatedStage):
    name = "review"
    task_kind = "review"
    config_sections = ("tasks", "workflow.review")

    def candidates(self, up: StageEval) -> list[dict[str, Any]]:
        return [it for it in up.output["items"] if it.get("keep")]

    def combine(self, up: StageEval, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        rows = {row["id"]: row for res in results.values() for row in res["items"]}
        items = []
        for it in up.output["items"]:
            row = rows.get(it["id"])
            if row is None:
                items.append(it)
                continue
            items.append({**it, "verdict": row["verdict"], "keep": row["verdict"] != "drop"})
        return {"items": items}


# --- merge -----------------------------------------------------------------


class MergeStage(BaseStage):
    name = "merge"
    config_sections = ("signals",)
    command = ("merge",)
    expect = "the merged item set"

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        return {"items": list(up.output["items"])}  # M1: pass-through


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
        """The operation an answer produces, or None when its action writes nothing."""
        if option.get("action") != "task.add":
            return None
        dest_id = self.target_destination(run, option)
        if dest_id is None:
            return None
        dest = run.ws.config.destinations[dest_id]
        ctx = item_context(item)
        ctx.update({
            "question_id": qid, "title": overrides.get("title") or ctx["title"], "due": overrides.get("due", ""),
            "est": overrides.get("est", ""), "when": overrides.get("when", ""), "note": overrides.get("note", ""),
            "area": "", "run": {"id": run.run.id, "date": run.run.header.clock.date},
        })
        line = templates.render(dest.line_template or DEFAULT_TASK_LINE, ctx)
        op = {"destination": dest_id, "verb": "append", "path": dest.path or f"{dest_id}.md",
              "anchor": dest.section or DEFAULT_SECTION, "line": line}
        op["op_id"] = "op_" + digest(op)[:16]
        op.update({"question_id": qid, "item_id": item["id"]})
        return op

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
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
        pd = digest(plan)
        pdir = run.run.paths.plan(pd)
        write_atomic(pdir / "plan.json", pretty_json(plan))
        diff = [f"# Plan {pd[:12]}", ""] + [f"- {o['destination']} ({o['path']} under {o['anchor']}): add `{o['line']}`"
                                           for o in ops]
        if not ops:
            diff.append("- No operations.")
        write_atomic(pdir / "diff.md", "\n".join(diff) + "\n")
        return {"plan_digest": pd, "operations": ops, "diff": pdir.relative_to(run.run.dir).as_posix() + "/diff.md"}


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
        return self.computational_eval(run, up, extra={"sandbox": run.run.header.sandbox})

    def destination(self, run: "RunContext", dest_id: str):
        from docket.destinations.markdown_tasks import MarkdownTasks
        cfg = run.ws.config.destinations[dest_id]
        live = MarkdownTasks(dest_id, cfg, run.ws.root)
        return live.sandbox(run.run.paths.sandbox) if run.run.header.sandbox else live

    def execute(self, run: "RunContext", ev: StageEval, up: StageEval | None) -> dict[str, Any]:
        from docket.apply import receipts
        ops = up.output["operations"]
        hdr = run.run.header
        if hdr.applied_plan != up.output["plan_digest"]:
            hdr.applied_plan = up.output["plan_digest"]
            run.run.save_header()
        rows = []
        for op in ops:
            prior = receipts.existing(run.run.store.db, op)
            if prior is not None and prior["verify_status"] == "verified":
                rows.append({"op_id": op["op_id"], "outcome": prior["outcome"], "verify_status": "verified"})
                continue
            dest = self.destination(run, op["destination"])
            try:
                res = dest.apply(op)
            except OSError as e:
                raise ExternalError("dest.apply_failed", f"{op['destination']}: {e}", hint=" ".join(run.argv("apply")),
                                    details={"op_id": op["op_id"], "error": str(e)}) from None
            ok = dest.verify(op)
            rec = receipts.record(run.run, op, res["outcome"], res["external_id"], ok)
            rows.append({"op_id": op["op_id"], "outcome": rec["outcome"], "verify_status": rec["verify_status"]})
        return {"plan_digest": up.output["plan_digest"], "sandbox": hdr.sandbox, "receipts": rows}


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
