"""Nodes (TD §5.1): what a graph is made of, and how each kind of node does its work.

A node does one thing, named by its verb. Its digest is a function of its own settings,
the configuration sections it reads, the output digests of the nodes it works on, the
answers it reads and the run clock. An artifact whose file name equals the fresh digest is
current; any other is stale. Nothing else tracks staleness.

How a node's work gets done:

- computed (read, run, merge, plan, apply, write, seal): Docket does the work when the
  agent runs ``docket do <node>``.
- judged (judge): done when every task the node issued has an accepted result.
- asked (ask, approve): done when the operator has answered every question.

Judged and asked outputs are pure functions of results and answers; they are computed in
memory (``docket next`` writes nothing) and written as artifacts by the next mutating
command.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from docket.cli.errors import ExternalError
from docket.config.digest import digest, pretty_json
from docket.config.models import Node
from docket.engine.steps import (
    OPTION, QUESTION, AskOption, AskQuestion, AskStep, Command, DispatchStep, RunStep, WaitStep,
)
from docket.store.paths import read_json, write_atomic
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

State = Literal["current", "stale", "pending", "waiting", "blocked"]
SEAL = "seal"
PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")


class InputRef(BaseModel):
    kind: Literal["artifact", "records", "answers"]
    node: str
    digest: str


class NodeStatus(BaseModel):
    """current | stale | pending (never built) | waiting(reason) | blocked(reason)."""
    node: str
    verb: str
    how: str
    state: State
    digest: str | None = None
    reason: str | None = None
    config_changed: bool | None = None


@dataclass
class Eval:
    """One node as it stands: its state, and its output when it is current."""
    name: str
    verb: str
    state: State
    digest: str | None = None
    reason: str | None = None
    output: dict[str, Any] | None = None
    output_digest: str | None = None
    config_digest: str | None = None
    inputs: list[InputRef] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    how: str = "computed"

    def status(self) -> NodeStatus:
        return NodeStatus(node=self.name, verb=self.verb, how=self.how, state=self.state, digest=self.digest,
                          reason=self.reason)


def join(evals: list[Eval]) -> Eval | None:
    """What several nodes hand on, as one: their outputs laid over each other in order. Items
    and context are joined by id (a later node adds to what an earlier one said of the same
    item); lists are joined end to end and maps key by key; anything else, the later wins."""
    if not evals:
        return None
    out: dict[str, Any] = {}
    items: dict[str, dict[str, Any]] = {}
    context: dict[str, dict[str, Any]] = {}
    sources: dict[str, Any] = {}
    has_items = False
    for ev in evals:
        data = ev.output or {}
        for k, v in data.items():
            if k == "items":
                has_items = True
                for it in v:
                    items[it["id"]] = {**items.get(it["id"], {}), **it}
            elif k == "context":
                for it in v:
                    context[it["id"]] = it
            elif k == "sources":
                sources.update(v)
            elif isinstance(v, list) and isinstance(out.get(k), list):
                out[k] = out[k] + v
            elif isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = {**out[k], **v}
            else:
                out[k] = v
    if has_items:
        out["items"] = [items[k] for k in sorted(items)]
        out["context"] = [context[k] for k in sorted(context)]
    if sources:
        out["sources"] = {k: sources[k] for k in sorted(sources)}
    refs = [InputRef(kind="artifact", node=ev.name, digest=ev.output_digest or "") for ev in evals]
    return Eval("+".join(ev.name for ev in evals), "join", "current", None, None, out,
                digest([r.digest for r in refs]), None, refs)


# ---------------------------------------------------------------------------


class BaseNode:
    verb = ""
    how = "computed"
    schema_version = 1
    config_sections: tuple[str, ...] = ()
    reads_time = False
    expect = ""

    def __init__(self, name: str, cfg: Node | None = None) -> None:
        self.name = name
        self.cfg = cfg

    # --- protocol ------------------------------------------------------------
    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        return RunStep(node=self.name, command=Command.of(run.argv("do", self.name)), why=self._why(ev),
                       expect=self.expect)

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        return self.computed_eval(run, up)

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        raise NotImplementedError

    # --- helpers -------------------------------------------------------------
    def _why(self, ev: Eval) -> str:
        if ev.state == "stale":
            return f"{self.name} is stale: what it works on or its configuration changed since it was built"
        return f"{self.name} has not run yet"

    def settings(self) -> Any:
        return self.cfg.model_dump(mode="json", by_alias=True, exclude={"description"}) if self.cfg is not None else None

    def sections(self, run: "RunContext") -> tuple[str, ...]:
        """The configuration sections this node reads; a change to any other leaves it current."""
        return self.config_sections

    def node_digest(self, run: "RunContext", inputs: list[InputRef], answers: str | None = None,
                    extra: Any = None) -> tuple[str, str]:
        cfg = digest([run.partial_config(self.sections(run)), self.settings()])
        body = {
            "verb": self.verb, "schema": self.schema_version, "config": cfg,
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

    def write_artifact(self, run: "RunContext", ev: Eval) -> None:
        body = {
            "node": self.name, "verb": self.verb, "digest": ev.digest, "schema": self.schema_version,
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

    def current_config_digest(self, run: "RunContext") -> str:
        return digest([run.partial_config(self.sections(run)), self.settings()])

    def up_input(self, up: Eval | None) -> list[InputRef]:
        return list(up.inputs) if up is not None else []

    def computed_eval(self, run: "RunContext", up: Eval | None, *, extra: Any = None) -> Eval:
        inputs = self.up_input(up)
        dg, cfg = self.node_digest(run, inputs, extra=extra)
        art = self.read_artifact(run, dg)
        if art is not None:
            return Eval(self.name, self.verb, "current", dg, None, art["data"], digest(art["data"]), cfg, inputs,
                        how=self.how)
        state: State = "stale" if self.has_any_artifact(run) else "pending"
        return Eval(self.name, self.verb, state, dg, "inputs changed" if state == "stale" else "not built",
                    None, None, cfg, inputs, how=self.how)

    def finish(self, run: "RunContext", ev: Eval, data: dict[str, Any]) -> Eval:
        ev.output, ev.output_digest, ev.state, ev.reason = data, digest(data), "current", None
        self.write_artifact(run, ev)
        return ev


# --- read --------------------------------------------------------------------


class ReadNode(BaseNode):
    """Read one source from its cursor (``docket.sources``)."""
    verb = "read"
    config_sections = ("sources", "transports")
    expect = "the source's records under read/<source>/, cursor proposals and the node's artifact"

    @property
    def source(self) -> str:
        assert self.cfg is not None and self.cfg.read is not None
        return self.cfg.read

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        from docket.sources.records import unavailable
        ev = self.computed_eval(run, up)
        if ev.state == "current":
            missing = unavailable(ev.output)
            if missing:
                ev.state, ev.reason = "waiting", "source_unavailable"
                ev.extra["unavailable"] = missing
        return ev

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        if ev.reason == "source_unavailable":
            row = ev.extra["unavailable"][self.source]
            text = f"{self.source}: {row.get('reason') or 'unavailable'}" + \
                (f" -> {row['instructions']}" if row.get("instructions") else "")
            return WaitStep(node=self.name, reason="source_unavailable",
                            instructions="A required source could not be read. " + text,
                            ready_when="the operator has fixed access to the source",
                            command=Command.of(run.argv("do", self.name, "--again")))
        return super().step(run)

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        from docket.sources.read import read_source
        assert self.cfg is not None
        return read_source(run, self.source, self.cfg.schedule)


# --- run ---------------------------------------------------------------------


class RunNode(BaseNode):
    """Run a command once. A sandbox run uses its ``dry_run`` form, or skips it when it has none."""
    verb = "run"
    expect = "the command's exit recorded"

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        return self.computed_eval(run, up, extra={"sandbox": run.run.header.sandbox})

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        assert self.cfg is not None
        r = run.run
        sandbox = r.header.sandbox
        tpl = self.cfg.dry_run if sandbox else self.cfg.run
        if tpl is None:
            return {"outcome": "skipped", "message": "no dry_run form declared; skipped in a sandbox run", "dry_run": True}
        ctx = {"run": {"id": r.id, "date": r.header.clock.date, "graph": r.header.graph, "dir": str(r.dir),
                       "sandbox": "true" if sandbox else ""}, "node": self.name, "workspace": str(run.ws.root)}
        argv = [templates.render(a, ctx) for a in tpl]
        env = {k: run.env[k] for k in (*PASS_ENV, *self.cfg.env) if k in run.env}
        env.update({"DOCKET_NONINTERACTIVE": "1", "DOCKET_RUN_ID": r.id, "DOCKET_RUN_DIR": str(r.dir),
                    "DOCKET_NODE": self.name, "DOCKET_WORKSPACE": str(run.ws.root)})
        log = r.dir / "logs" / "run" / f"{self.name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        code: int | None = None
        with log.open("ab") as fh:
            fh.write(f"--- {self.name}: {' '.join(argv)}\n".encode())
            fh.flush()
            try:
                proc = subprocess.Popen(argv, cwd=str(run.ws.root), env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = proc.wait(timeout=self.cfg.timeout_s)
                    msg = None if code == 0 else f"exited with status {code}"
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, 9)
                    proc.wait()
                    msg = f"timed out after {self.cfg.timeout_s:g}s"
            except OSError as e:
                msg = f"could not start {argv[0]}: {e.strerror or e}"
        if code != 0:
            again = run.argv("do", self.name)
            raise ExternalError("node.command_failed", f"Node {self.name} {msg}.", retryable=True,
                                hint=" ".join(again), next_command=again,
                                details={"node": self.name, "argv": argv, "exit": code, "log": str(log)})
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
        return {"outcome": "ok", "argv": argv, "dry_run": sandbox, "elapsed_s": round(time.monotonic() - t0, 3),
                "log": log.relative_to(r.dir).as_posix(), "last_line": tail[0][:400] if tail else ""}


# --- judge -------------------------------------------------------------------


class JudgeNode(BaseNode):
    """An agent judges what the node works on, one task kind per node (``docket.tasks``).

    What the node hands on depends on the kind's family. ``assess`` hands on every item with
    the judgment on it. The other item kinds (``recap``, ``extract``, ``classify``, a
    workspace kind) hand on the items with their rows attached. ``review`` hands on the
    items with the reviewer's verdicts applied. ``label`` works on decisions and hands on
    the labels alone.
    """
    verb = "judge"
    how = "judged"
    config_sections = ("profile", "signals", "tasks", "planning", "destinations")

    def sections(self, run: "RunContext") -> tuple[str, ...]:
        # An item kind reads the rules that select its items (recap also the agendas it reads
        # beside them), review only the tasks, and label the task lines planning may place.
        kind = self.kind(run)
        if kind.family == "review":
            return ("profile", "tasks")
        if kind.family == "label":
            return ("profile", "tasks", "planning", "destinations")
        if kind.name == "recap":
            return ("profile", "signals", "tasks", "destinations")
        return ("profile", "signals", "tasks")

    @property
    def task(self) -> str:
        assert self.cfg is not None and self.cfg.judge is not None
        return self.cfg.judge

    def kind(self, run: "RunContext") -> Any:
        from docket.tasks.kinds import kinds_for
        return kinds_for(run.ws.config)[self.task]

    def candidates(self, run: "RunContext", up: Eval | None) -> list[dict[str, Any]]:
        kind = self.kind(run)
        data = (up.output if up is not None else None) or {}
        if kind.family == "label":
            from docket.plan.labels import candidates
            return candidates(run, data)
        items = list(data.get("items") or [])
        if kind.family == "review":
            return [it for it in items if it.get("keep")]
        from docket.signals.node import assess_selection
        cands = assess_selection(run, items).candidates
        if kind.name == "recap":
            from docket.plan.agenda_roll import with_agendas
            cands = with_agendas(run, cands)
        return cands

    def combine(self, run: "RunContext", up: Eval | None, plan: Any) -> dict[str, Any]:
        from docket.tasks import runner
        kind = self.kind(run)
        data = (up.output if up is not None else None) or {}
        if kind.family == "label":
            rows = runner.rows(plan)
            return {"labels": {tid: str(row.get("label") or "") for tid, (_, row) in sorted(rows.items())}}
        items = list(data.get("items") or [])
        rest = {k: v for k, v in data.items() if k != "items"}
        if kind.family == "review":
            return {**rest, **runner.combine_review(items, plan)}
        if kind.name == "assess":
            return {**rest, **runner.combine_assess(items, plan)}
        return {**rest, **runner.combine_rows(items, plan, kind.name)}

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        from docket.tasks.runner import plan_node
        inputs = self.up_input(up)
        dg, cfg = self.node_digest(run, inputs)
        plan = plan_node(run, self.name, self.kind(run), self.candidates(run, up))
        extra = {"packs": plan.packs, "issued": plan.issued, "accepted": set(plan.results), "plan": plan}
        if plan.complete:
            out = self.combine(run, up, plan)
            return Eval(self.name, self.verb, "current", dg, None, out, digest(out), cfg, inputs, extra, self.how)
        return Eval(self.name, self.verb, "waiting", dg, "tasks", None, None, cfg, inputs, extra, self.how)

    def step(self, run: "RunContext") -> Any:
        from docket.tasks.runner import dispatch_tasks, dispatch_why
        ev = run.eval(self.name)
        plan = ev.extra["plan"]
        if plan.pending_issue():
            return RunStep(node=self.name, command=Command.of(run.argv("task", "issue")),
                           why=f"{self.name} needs {len(plan.pending_issue())} task pack(s) written before dispatch",
                           expect="pack.json and prompt.md per task", timeout_s=60)
        tasks = dispatch_tasks(run, plan)
        return DispatchStep(node=self.name, tasks=tasks, concurrency=run.ws.config.tasks.concurrency,
                            why=dispatch_why(len(tasks), self.name, self.task))

    def issue(self, run: "RunContext") -> list[str]:
        from docket.tasks.runner import issue
        return issue(run, run.eval(self.name).extra["plan"])


# --- merge -------------------------------------------------------------------


class MergeNode(BaseNode):
    """Rules, scores, merge by topic, suppression and coverage (``docket.signals``)."""
    verb = "merge"
    schema_version = 3  # 2: conversation topics. 3: an open commitment is not an answer
    config_sections = ("profile", "signals", "destinations")
    expect = "the merge artifact: topics with scores, statuses, traces and coverage notes"

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        from docket.signals.node import merge_extra
        return self.computed_eval(run, up, extra=merge_extra(run))

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        from docket.signals.node import execute_merge
        return execute_merge(run, up)


# --- ask ---------------------------------------------------------------------


class AskNode(BaseNode):
    """The operator decides: questions, options and answers live in ``docket.decide`` (TD §10).

    The node digest folds in the digest of the questions themselves, so what the planning
    policy, the calendar, the task store and destination capacity do to questions stales
    the node without declaring those sections (a scheduling-only edit leaves it current).
    """
    verb = "ask"
    how = "asked"
    config_sections = ("decisions", "profile", "signals")

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        from docket.decide.answers import bind
        from docket.decide.questions import build
        assert self.cfg is not None
        items = list(((up.output if up is not None else None) or {}).get("items") or [])
        qs = build(run, items, self.cfg)
        bound, previous = bind(qs.questions, run.run.answers())
        for q in qs.questions:
            if q["id"] in previous:
                q["previous"] = previous[q["id"]]
        shown = [{k: v for k, v in q.items() if k != "previous"} for q in qs.questions]
        inputs = self.up_input(up)
        dg, cfg = self.node_digest(run, inputs, answers=digest(bound),
                                   extra={"questions": digest(shown), "pending_refs": qs.pending_refs})
        extra = {"questions": qs.questions, "answers": bound, "previous": previous, "set": qs}
        if qs.pending_refs:
            return Eval(self.name, self.verb, "waiting", dg, "resolve", None, None, cfg, inputs, extra, self.how)
        if len(bound) == len(qs.questions):
            out = {
                "questions": shown, "answers": bound, "findings": qs.findings, "reconcile": qs.reconcile,
                "verify": qs.verify, "not_asked": qs.not_asked,
                "merged": {k: v["merged_into"] for k, v in sorted(bound.items()) if v.get("merged_into")},
            }
            return Eval(self.name, self.verb, "current", dg, None, out, digest(out), cfg, inputs, extra, self.how)
        return Eval(self.name, self.verb, "waiting", dg, "answers", None, None, cfg, inputs, extra, self.how)

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        if ev.reason == "resolve":
            refs = ev.extra["set"].pending_refs
            return RunStep(node=self.name, command=Command.of(run.argv("ask", "resolve")),
                           why=f"{len(refs)} task reference(s) must be resolved through their sources before the "
                               f"questions are asked, so the burst needs no source reads",
                           expect="resolutions.json with one resolution per reference", timeout_s=120)
        pending = [q for q in ev.extra["questions"] if q["id"] not in ev.extra["answers"]]
        reasked = sum(1 for q in pending if q.get("previous"))
        why = f"{len(pending)} question(s) need the operator's answer; ask them, then record each answer"
        if reasked:
            why += f" ({reasked} re-asked because their item changed; each shows its previous answer)"
        return ask_step(run, self.name, pending, why=why)


def ask_step(run: "RunContext", node: str, questions: list[dict[str, Any]], why: str,
             extra_args: list[str] | None = None) -> AskStep:
    """The full ask step; ``docket next`` shows its compact form unless ``--full`` is given."""
    out = []
    extra = list(extra_args or [])
    for q in questions:
        opts = []
        for i, o in enumerate(q["options"], 1):
            argv = run.argv("ask", "answer", q["id"], o["id"], *extra)
            opts.append(AskOption(id=o["id"], label=o["label"], position=i, action=o.get("action"),
                                  answer=Command.of(argv)))
        details = {k: v for k, v in q.items() if k in ("item_id", "digest", "diff_path", "operations", "counts",
                                                        "block_key", "task_id", "evidence", "refused", "notes",
                                                        "previous", "requires", "defaults", "recommended")
                   and v not in (None, [], {})}
        batch = [o["id"] for o in q["options"] if o.get("batch")]
        if batch:
            details["batch"] = batch
        out.append(AskQuestion(id=q["id"], kind=q["kind"], prompt=q["prompt"], options=opts, details=details))
    template = Command.of(run.argv("ask", "answer", QUESTION, OPTION, *extra))
    show = Command.of(run.argv("ask", "show", QUESTION))
    why = (f"{why}. Answer each with the answer command: replace {QUESTION} with the question id and {OPTION} "
           f"with one of that question's option ids; every other word is concrete. For a question's evidence, "
           f"defaults and refused options run the show command (docket ask show {QUESTION}), or docket next --full")
    return AskStep(node=node, questions=out, answer=template, show=show, full=True, why=why)


# --- plan --------------------------------------------------------------------

EMPTY_DECISIONS: dict[str, Any] = {"questions": [], "answers": {}, "findings": [], "reconcile": [], "verify": [],
                                   "not_asked": [], "merged": {}}


class PlanNode(BaseNode):
    """Turn the operator's decisions into operations, and with ``schedule_tasks`` place the
    open tasks on the calendar (``docket.decide.operations``, ``docket.plan``)."""
    verb = "plan"
    config_sections = ("planning", "destinations", "decisions")
    reads_time = True
    expect = "a plan with its digest under plan/<digest>/ and a diff to show the operator"

    def decisions(self, up: Eval | None) -> dict[str, Any]:
        return {**EMPTY_DECISIONS, **((up.output if up is not None else None) or {})}

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        from docket.apply.freshness import rebuild_token
        from docket.plan.build import input_digests, planning_active
        extra: Any = input_digests(run) if planning_active(run, self.name) else None
        token = rebuild_token(run)  # an approval refused for stale destinations stales the plan (TD §13.2)
        if token is not None:
            extra = {"planning": extra, "rebuild": token}
        return self.computed_eval(run, up, extra=extra)

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        from zoneinfo import ZoneInfo
        from docket.apply import freshness
        from docket.decide.operations import plan_operations
        from docket.plan.build import extend_plan, planning_active, render_diff
        decide = self.decisions(up)
        mapped = plan_operations(run, decide)  # every answer kind (TD §10.3)
        ops = sorted(mapped["operations"], key=lambda o: (o["destination"], o["op_id"]))
        plan = {
            "inputs": {"decisions": up.output_digest if up is not None else None, "config_digest": ev.config_digest},
            "operations": ops, "schedule": [], "unschedulable": [], "notes": list(mapped["notes"]),
            "outcomes": mapped["outcomes"],
        }
        scheduling = planning_active(run, self.name)
        built = extend_plan(run, plan, decide, plan["inputs"]["decisions"]) if scheduling else None
        snaps = freshness.attach(run, plan)  # inputs.destinations (TD §13.2)
        pd = digest(plan)
        pdir = run.run.paths.plan(pd)
        write_atomic(pdir / "plan.json", pretty_json(plan))
        freshness.save(pdir, snaps)
        out = {"plan_digest": pd, "operations": plan["operations"], "outcomes": plan["outcomes"],
               "diff": pdir.relative_to(run.run.dir).as_posix() + "/diff.md"}
        if built is not None:
            write_atomic(pdir / "diff.md", render_diff(plan, pd, ZoneInfo(run.run.header.clock.timezone)))
            out["planning"] = {"blocks": len(plan["schedule"]), "unschedulable": len(plan["unschedulable"]),
                               "calendar_operations": len(built.ops)}
            return out
        diff = [f"# Plan {pd[:12]}", ""] + [op_line(o) for o in ops]
        if not ops:
            diff.append("- No operations.")
        diff += _outcome_lines(plan)
        write_atomic(pdir / "diff.md", "\n".join(diff) + "\n")
        return out


def op_line(o: dict[str, Any]) -> str:
    if o.get("verb") == "replace":
        return f"- {o['destination']} ({o['path']} under {o['anchor']}): change `{o.get('old_line', '')}` to `{o['line']}`"
    if o.get("verb") == "roll":
        n = len((o.get("body") or "").split("\n"))
        return (f"- {o['destination']} ({o['path']}): roll `{o.get('title', '')}`, {n} line(s); "
                f"{len(o.get('remove') or [])} entr(ies) leave {o.get('anchor')}")
    if o.get("verb") == "draft":
        return f"- {o['destination']}: draft `{o.get('subject', '')}` to {', '.join(o.get('to') or [])}"
    return f"- {o['destination']} ({o.get('path')} under {o.get('anchor')}): add `{o.get('line')}`"


def _outcome_lines(plan: dict[str, Any]) -> list[str]:
    rows = plan.get("outcomes") or []
    if not rows:
        return []
    return ["", "## Decisions without operations"] + [
        f"- {r['question']}: {r['decision']}" + (f" until {r['until']}" if r.get("until") else "")
        + (f" ({r['note']})" if r.get("note") else "") for r in rows]


# --- approve -----------------------------------------------------------------


class ApproveNode(BaseNode):
    """The operator approves a plan. The approval is bound to the plan's digest; the question's
    id is the node's name."""
    verb = "approve"
    how = "asked"

    def question(self, run: "RunContext", up: Eval) -> dict[str, Any]:
        pd = up.output["plan_digest"]
        n = len(up.output["operations"])
        diff = str(run.run.dir / up.output["diff"])
        counts: dict[str, int] = {}
        for op in up.output["operations"]:
            counts[op["destination"]] = counts.get(op["destination"], 0) + 1
        per = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        return {
            "id": self.name, "kind": "approve", "digest": pd, "diff_path": diff, "operations": n,
            "counts": dict(sorted(counts.items())),
            "prompt": f"Approve the plan ({n} operation(s){': ' + per if per else ''}; digest {pd[:12]})? "
                      f"Show the operator {diff} first.",
            "options": [{"id": "yes", "label": "Approve", "action": "approve"},
                        {"id": "no", "label": "Reject", "action": "reject"}],
        }

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        assert up is not None and up.output is not None
        q = self.question(run, up)
        ans = run.run.answers().get(self.name)
        bound = ans if ans and ans.get("digest") == q["digest"] else None
        inputs = self.up_input(up)
        dg, cfg = self.node_digest(run, inputs, answers=digest(bound))
        extra = {"question": q, "answer": bound}
        if bound and bound.get("option") == "yes":
            out = {"approved": True, "plan_digest": q["digest"], "operations": up.output["operations"]}
            return Eval(self.name, self.verb, "current", dg, None, out, digest(out), cfg, inputs, extra, self.how)
        if bound:
            return Eval(self.name, self.verb, "blocked", dg, "plan_rejected", None, None, cfg, inputs, extra, self.how)
        return Eval(self.name, self.verb, "waiting", dg, "answers", None, None, cfg, inputs, extra, self.how)

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        q = ev.extra["question"]
        if ev.reason == "plan_rejected":
            return WaitStep(node=self.name, reason="plan_rejected",
                            instructions="The operator rejected the plan. Change answers with docket ask answer, "
                                         "rebuild the plan, or approve with the yes command.",
                            command=Command.of(run.argv("ask", "answer", self.name, "yes", "--digest", q["digest"])))
        return ask_step(run, self.name, [q],
                        why="Show the operator the plan diff and record approval bound to the plan digest",
                        extra_args=["--digest", q["digest"]])


# --- apply -------------------------------------------------------------------


def _failed(run: "RunContext", node: BaseNode, ev: Eval, data: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    failed = [r for r in rows if r["outcome"] == "failed"]
    if not failed:
        return
    node.finish(run, ev, data)  # keep what completed; docket next now waits with apply_partial
    first = failed[0].get("error") or {}
    again = run.argv("do", node.name, "--again")
    raise ExternalError("dest.apply_failed",
                        f"{len(failed)} operation(s) failed; first: {first.get('message', 'unknown error')}",
                        hint=" ".join(again), retryable=True, next_command=again,
                        details={"failed": failed, "receipts": len(rows)})


def _partial_step(run: "RunContext", name: str, ev: Eval) -> WaitStep:
    rows = [r for r in ev.output["receipts"] if r.get("outcome") in ("failed", "blocked")]
    return WaitStep(node=name, reason="apply_partial",
                    instructions=f"{len(rows)} operation(s) failed or are blocked by a failed operation; completed "
                                 f"operations will not repeat. Fix the cause and run the retry command.",
                    command=Command.of(run.argv("do", name, "--again")))


class ApplyNode(BaseNode):
    """Write the operations the operator approved (``docket.apply``)."""
    verb = "apply"
    config_sections = ("destinations",)
    expect = "one receipt per operation in receipts.jsonl"

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        ev = self.computed_eval(run, up, extra={"sandbox": run.run.header.sandbox})
        if ev.state == "current" and any(r.get("outcome") == "failed" for r in (ev.output or {}).get("receipts", [])):
            # an external failure can succeed on retry: hold the node (TD §13.3). A refusal is final.
            ev.state, ev.reason = "waiting", "apply_partial"
        return ev

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        return _partial_step(run, self.name, ev) if ev.reason == "apply_partial" else super().step(run)

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        from docket.apply import boundary, executor
        assert up is not None and up.output is not None
        boundary.pin(run, self.name, up.output["plan_digest"])  # TD §5.4: from here the run never goes backward
        rows = executor.execute(run, up.output["operations"])
        data = {"plan_digest": up.output["plan_digest"], "sandbox": run.run.header.sandbox, "receipts": rows}
        _failed(run, self, ev, data, rows)
        return data


# --- write -------------------------------------------------------------------


class WriteNode(BaseNode):
    """Plan and write operations that need no approval. The operations go to one destination,
    which must be private; the plan is kept beside the receipts all the same.

    ``write: roll`` rolls agendas after the meetings the node works on (TD §12.8), and records
    what the operator agreed to do in them for a source to read.
    """
    verb = "write"
    config_sections = ("destinations", "profile")
    expect = "the operations written, one receipt each"

    def evaluate(self, run: "RunContext", up: Eval | None) -> Eval:
        ev = self.computed_eval(run, up, extra={"sandbox": run.run.header.sandbox})
        if ev.state == "current" and any(r.get("outcome") == "failed" for r in (ev.output or {}).get("receipts", [])):
            ev.state, ev.reason = "waiting", "apply_partial"
        return ev

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        return _partial_step(run, self.name, ev) if ev.reason == "apply_partial" else super().step(run)

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        from docket.apply import boundary, executor
        from docket.plan import agenda_roll
        assert self.cfg is not None and up is not None
        items = list((up.output or {}).get("items") or [])
        meetings = agenda_roll.meetings_of(run, items)
        ops = agenda_roll.operations(run, self.cfg, meetings)
        promised = agenda_roll.commitments(meetings)
        plan = {"inputs": {"items": up.output_digest, "config_digest": ev.config_digest}, "operations": ops,
                "commitments": promised}
        pd = digest(plan)
        pdir = run.run.paths.plan(pd)
        write_atomic(pdir / "plan.json", pretty_json(plan))
        write_atomic(pdir / "diff.md", "\n".join([f"# Plan {pd[:12]}", ""] + [op_line(o) for o in ops]
                                                 + ([] if ops else ["- No operations."])) + "\n")
        boundary.pin(run, self.name, pd)
        rows = executor.execute(run, ops)
        data = {"plan_digest": pd, "sandbox": run.run.header.sandbox, "operations": ops, "receipts": rows,
                "commitments": 0}
        _failed(run, self, ev, data, rows)
        if promised and self.cfg.commitments and not run.run.header.sandbox:
            from pathlib import Path
            path = Path(self.cfg.commitments).expanduser()
            data["commitments"] = agenda_roll.append_commitments(path if path.is_absolute() else run.ws.root / path,
                                                                 promised)
        return data


# --- seal --------------------------------------------------------------------


class SealNode(BaseNode):
    """The end of every run: cursors, dispositions, the ledger and the journal, each step
    receipted (``docket.seal.run``). It is not written in a graph; every graph ends with it."""
    verb = "seal"
    config_sections = ("journal", "sources")
    expect = "cursors advanced, dispositions and the ledger written, journal.md written and the run sealed"

    def step(self, run: "RunContext") -> Any:
        ev = run.eval(self.name)
        return RunStep(node=self.name, command=Command.of(run.argv("seal")), why=self._why(ev), expect=self.expect)

    def execute(self, run: "RunContext", ev: Eval, up: Eval | None) -> dict[str, Any]:
        from docket.seal.run import execute_seal
        return execute_seal(run)


KINDS: dict[str, type[BaseNode]] = {
    "read": ReadNode, "run": RunNode, "judge": JudgeNode, "merge": MergeNode, "ask": AskNode, "plan": PlanNode,
    "approve": ApproveNode, "apply": ApplyNode, "write": WriteNode,
}
SCHEMAS: dict[str, int] = {**{v: k.schema_version for v, k in KINDS.items()}, SEAL: SealNode.schema_version}


def build(name: str, cfg: Node | None) -> BaseNode:
    return SealNode(name) if cfg is None else KINDS[cfg.verb](name, cfg)
