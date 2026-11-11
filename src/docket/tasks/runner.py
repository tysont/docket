"""A judge node's task work (TD §5.1, §9): plan, issue, dispatch, submit and combine.

A judge node (``docket.engine.nodes``) issues tasks of one kind over what it works on. It
is done when every pack has an accepted result; its output is a pure function of its
input and those results.

Review verdicts wire into the item set:

- ``ask``: the item stays kept; ``against`` travels with it to the question;
- ``drop``: the item is no longer kept; the reason is in its trace;
- ``merge``: the item is no longer kept and names the retained item it merges
  into (``merged_into``); that item lists it under ``merged``. A merge whose
  target is not retained keeps the item, so no finding is lost;
- ``needs_verification``: the item stays kept and is flagged for the operator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from docket.config.digest import pretty_json
from docket.engine.steps import Command, DispatchTask
from docket.store.paths import write_atomic
from docket.tasks import index
from docket.tasks.evidence import RecordTexts, record_ids
from docket.tasks.kinds import TaskKind
from docket.tasks.packs import PackContext, build_packs, load_units

if TYPE_CHECKING:
    from docket.engine.context import RunContext


@dataclass
class NodePlan:
    node: str
    kind: TaskKind
    packs: list[dict[str, Any]]
    results: dict[str, dict[str, Any]]
    issued: set[str]
    records: dict[str, list[str]] = field(default_factory=dict)        # item id -> raw record ids

    @property
    def complete(self) -> bool:
        return all(p["task_id"] in self.results for p in self.packs)

    def pending_issue(self) -> list[dict[str, Any]]:
        return [p for p in self.packs if p["task_id"] not in self.issued and p["task_id"] not in self.results]

    def pack(self, task_id: str) -> dict[str, Any] | None:
        return next((p for p in self.packs if p["task_id"] == task_id), None)


def plan_node(rc: "RunContext", node: str, kind: TaskKind, candidates: list[dict[str, Any]]) -> NodePlan:
    """Every pack the node needs for ``candidates``, with the results already accepted."""
    ctx = PackContext.build(rc.ws.config, load_units(rc.run.store.db, {str(it.get("source")) for it in candidates}))
    packs = build_packs(kind, candidates, ctx)
    results = index.accepted(rc, packs)
    issued = {p["task_id"] for p in packs if (rc.run.paths.task(p["task_id"]) / "pack.json").is_file()}
    records = {it["id"]: record_ids(it) for it in candidates if "id" in it}
    return NodePlan(node, kind, packs, results, issued, records)


# --- issue and dispatch ------------------------------------------------------------


def result_path(rc: "RunContext", task_id: str) -> str:
    return str(rc.run.paths.task(task_id) / "submission.json")


def submit_argv(rc: "RunContext", task_id: str) -> list[str]:
    return rc.argv("task", "submit", task_id, result_path(rc, task_id))


def issue(rc: "RunContext", plan: NodePlan) -> list[str]:
    """Write pack.json and prompt.md for every pack that is neither issued nor accepted."""
    from docket.tasks.prompts import render_prompt
    written = []
    for p in plan.pending_issue():
        tdir = rc.run.paths.task(p["task_id"])
        write_atomic(tdir / "pack.json", pretty_json({**p, "node": plan.node}))
        write_atomic(tdir / "prompt.md", render_prompt(
            plan.kind, p, config=rc.ws.config, result_path=result_path(rc, p["task_id"]),
            submit_shell=Command.of(submit_argv(rc, p["task_id"])).shell))
        written.append(p["task_id"])
    return written


def dispatch_tasks(rc: "RunContext", plan: NodePlan) -> list[DispatchTask]:
    out = []
    for p in plan.packs:
        if p["task_id"] in plan.results:
            continue
        tdir = rc.run.paths.task(p["task_id"])
        out.append(DispatchTask(
            task_id=p["task_id"], kind=p["kind"], pack_path=str(tdir / "pack.json"),
            prompt_path=str(tdir / "prompt.md"), result_path=result_path(rc, p["task_id"]),
            result_schema=plan.kind.output_schema, submit=Command.of(submit_argv(rc, p["task_id"])),
        ))
    return out


def dispatch_why(n: int, node: str, kind: str) -> str:
    return (f"{node}: {n} independent {kind} task(s) need results. Each task is self-contained: run "
            f"them as parallel subagents up to the concurrency, or one after another inline in this session when "
            f"subagents are not available; the prompt and submit command are the same either way. Write each "
            f"result to its result_path and run its submit command.")


# --- submit ------------------------------------------------------------------------


def validate_submission(rc: "RunContext", plan: NodePlan, pack: dict[str, Any], payload: Any) -> Any:
    from docket.tasks.submit import validate
    kind = plan.kind
    texts = RecordTexts(rc, {str(it.get("source")) for it in pack["items"]})
    rerun = Command.of(submit_argv(rc, pack["task_id"])).shell
    return validate(kind, pack, payload, raw_text=lambda iid: texts.for_records(plan.records.get(iid, [])),
                    rerun=rerun)


# --- combine -------------------------------------------------------------------------


def rows(plan: NodePlan) -> dict[str, tuple[str, dict[str, Any]]]:
    """item id -> (task id, row), over the node's accepted results."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for p in plan.packs:
        res = plan.results.get(p["task_id"])
        if res is None:
            continue
        for row in res.get("items", []):
            out[row["id"]] = (p["task_id"], row)
    return out


def _trace(it: dict[str, Any], entry: dict[str, Any]) -> list[Any]:
    tr = it.get("trace")
    return [*(tr if isinstance(tr, list) else []), entry]


def _body(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "id"}


def combine_assess(items: list[dict[str, Any]], plan: NodePlan) -> dict[str, Any]:
    """Every item with the assessment on it. An item no task judged is not kept."""
    assess = rows(plan)
    out = []
    for it in items:
        new = dict(it)
        hit = assess.get(it["id"])
        row = hit[1] if hit else {}
        new["keep"] = bool(row.get("keep"))
        new["score"] = int(row.get("score") or 0)
        if hit:
            new["assessment"] = _body(row)
            new["trace"] = _trace(it, {"node": plan.node, "task": hit[0], "keep": new["keep"],
                                       "score": new["score"], "kind": row.get("kind")})
        out.append(new)
    return {"items": out}


def combine_rows(items: list[dict[str, Any]], plan: NodePlan, kind: str) -> dict[str, Any]:
    """Every item, with the kind's row attached where a task judged it: an extraction's
    entries under ``extracted``, a classification under ``classified``, any other kind
    under ``tasks.<kind>``."""
    got = rows(plan)
    out = []
    for it in items:
        new = dict(it)
        hit = got.get(it["id"])
        if hit:
            row = hit[1]
            if kind == "extract":
                new["extracted"] = list(row.get("entries") or [])
            elif kind == "classify":
                new["classified"] = _body(row)
            else:
                new["tasks"] = {**(new.get("tasks") if isinstance(new.get("tasks"), dict) else {}), kind: _body(row)}
        out.append(new)
    return {"items": out}


def combine_review(items: list[dict[str, Any]], plan: NodePlan) -> dict[str, Any]:
    verdicts: dict[str, tuple[str | None, dict[str, Any]]] = dict(rows(plan))
    by_id = {it["id"]: it for it in items}

    def retained(iid: str) -> bool:
        it = by_id.get(iid)
        if it is None or not it.get("keep"):
            return False
        v = verdicts.get(iid)
        return v is None or v[1].get("verdict") not in ("drop", "merge")

    def root(iid: str) -> str | None:
        seen = {iid}
        cur = verdicts.get(iid, (None, {}))[1].get("merge_with")
        while isinstance(cur, str) and cur not in seen:
            seen.add(cur)
            v = verdicts.get(cur)
            if v is None or v[1].get("verdict") != "merge" or not v[1].get("merge_with"):
                return cur
            cur = v[1]["merge_with"]
        return None

    merged_into: dict[str, str] = {}
    for iid, (_, row) in sorted(verdicts.items()):
        if row.get("verdict") == "merge" and iid in by_id:
            target = root(iid)
            if target is not None and target != iid and retained(target):
                merged_into[iid] = target
    absorbed: dict[str, list[str]] = {}
    for src, dst in sorted(merged_into.items()):
        absorbed.setdefault(dst, []).append(src)

    out = []
    for it in items:
        iid = it["id"]
        new = dict(it)
        hit = verdicts.get(iid)
        if hit is None:
            out.append(new)
            continue
        task, row = hit
        v = row.get("verdict")
        review = {k: row[k] for k in ("against", "reason", "merge_with", "decision") if row.get(k)}
        entry: dict[str, Any] = {"node": plan.node, "verdict": v, **review}
        if row.get("decision"):
            # The reviewer's decision replaces the first recommendation; the trace keeps both.
            was = (it.get("assessment") or {}).get("decision")
            new["assessment"] = {**(it.get("assessment") or {}), "decision": row["decision"]}
            entry["was"] = was
        entry["task"] = task
        new["verdict"] = v
        new["review"] = review
        if v == "drop":
            new["keep"] = False
            new["dropped"] = {"node": plan.node, "reason": row.get("reason") or ""}
        elif v == "merge":
            if iid in merged_into:
                new["keep"] = False
                new["merged_into"] = merged_into[iid]
                entry["merged_into"] = merged_into[iid]
            else:
                entry["note"] = "merge target not retained; the item stays"
        elif v == "needs_verification":
            new["needs_verification"] = True
        if iid in absorbed:
            new["merged"] = sorted({*(it.get("merged") if isinstance(it.get("merged"), list) else []),
                                    *absorbed[iid]})
        new["trace"] = _trace(it, entry)
        out.append(new)
    return {"items": out}
