"""Delegated-stage task work (TD §5.1, §9): plan, issue, dispatch, submit and combine.

The ``assess`` and ``review`` stages (``docket.engine.stages``) call into this
module. A stage issues its core kind and every other kind declared for it
(``extract``, ``classify`` and workspace kinds once configured). It is
complete when every pack has an accepted result; its output is a pure function
of its input items and those results.

Review verdicts wire into the item set:

- ``ask``: the item stays kept; ``against`` travels with it to the question;
- ``drop``: the item is no longer kept; the reason is in its trace;
- ``merge``: the item is no longer kept and names the retained item it merges
  into (``merged_into``); that item lists it under ``merged``. A merge whose
  target is not retained keeps the item, so no finding is lost;
- ``needs_verification``: the item stays kept and is flagged for decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from docket.config.digest import pretty_json
from docket.engine.steps import Command, DispatchTask
from docket.store.paths import write_atomic
from docket.tasks import index
from docket.tasks.evidence import RecordTexts, record_ids
from docket.tasks.kinds import TaskKind, kinds_for, stage_kinds
from docket.tasks.packs import PackContext, build_packs, carry_key, item_view, load_units

if TYPE_CHECKING:
    from docket.engine.context import RunContext


@dataclass
class StagePlan:
    stage: str
    kinds: dict[str, TaskKind]
    packs: list[dict[str, Any]]
    results: dict[str, dict[str, Any]]
    issued: set[str]
    generation: int
    carried: dict[str, dict[str, Any]] = field(default_factory=dict)   # item id -> carried review row
    records: dict[str, list[str]] = field(default_factory=dict)        # item id -> raw record ids

    @property
    def complete(self) -> bool:
        return all(p["task_id"] in self.results for p in self.packs)

    def pending_issue(self) -> list[dict[str, Any]]:
        return [p for p in self.packs if p["task_id"] not in self.issued and p["task_id"] not in self.results]

    def pack(self, task_id: str) -> dict[str, Any] | None:
        return next((p for p in self.packs if p["task_id"] == task_id), None)


def _generation(rc: "RunContext") -> int:
    from docket.sources.generations import current_generation
    return current_generation(rc)


def plan_stage(rc: "RunContext", stage: str, candidates: list[dict[str, Any]]) -> StagePlan:
    """Every pack the stage needs for ``candidates``, with the results already accepted."""
    cfg = rc.ws.config
    kinds = stage_kinds(cfg, stage)
    gen = _generation(rc)
    ctx = PackContext.build(cfg, load_units(rc.run.store.db, {str(it.get("source")) for it in candidates}))
    packs: list[dict[str, Any]] = []
    carried: dict[str, dict[str, Any]] = {}
    for kind in kinds:
        assert kind.select is not None
        chosen = [it for it in candidates if kind.select.matches(it)]
        if kind.name == "review" and gen > 0 and chosen:
            earlier = index.carried_rows(rc, kind, gen)
            if earlier:
                keep = []
                for it in chosen:
                    row = earlier.get(carry_key(kind, item_view(kind, it)))
                    if row is not None:
                        carried[it["id"]] = row
                    else:
                        keep.append(it)
                chosen = keep
        packs += build_packs(kind, chosen, ctx)
    results = index.accepted(rc, packs)
    issued = {p["task_id"] for p in packs if (rc.run.paths.task(p["task_id"]) / "pack.json").is_file()}
    records = {it["id"]: record_ids(it) for it in candidates if "id" in it}
    return StagePlan(stage, {k.name: k for k in kinds}, packs, results, issued, gen, carried, records)


# --- issue and dispatch ------------------------------------------------------------


def result_path(rc: "RunContext", task_id: str) -> str:
    return str(rc.run.paths.task(task_id) / "submission.json")


def submit_argv(rc: "RunContext", task_id: str) -> list[str]:
    return rc.argv("task", "submit", task_id, result_path(rc, task_id))


def issue(rc: "RunContext", plan: StagePlan) -> list[str]:
    """Write pack.json and prompt.md for every pack that is neither issued nor accepted."""
    from docket.tasks.prompts import render_prompt
    written = []
    for p in plan.pending_issue():
        kind = plan.kinds[p["kind"]]
        tdir = rc.run.paths.task(p["task_id"])
        write_atomic(tdir / "pack.json", pretty_json({**p, "issued_generation": plan.generation}))
        write_atomic(tdir / "prompt.md", render_prompt(
            kind, p, config=rc.ws.config, result_path=result_path(rc, p["task_id"]),
            submit_shell=Command.of(submit_argv(rc, p["task_id"])).shell))
        written.append(p["task_id"])
    return written


def dispatch_tasks(rc: "RunContext", plan: StagePlan) -> list[DispatchTask]:
    out = []
    for p in plan.packs:
        if p["task_id"] in plan.results:
            continue
        tdir = rc.run.paths.task(p["task_id"])
        out.append(DispatchTask(
            task_id=p["task_id"], kind=p["kind"], pack_path=str(tdir / "pack.json"),
            prompt_path=str(tdir / "prompt.md"), result_path=result_path(rc, p["task_id"]),
            result_schema=plan.kinds[p["kind"]].output_schema, submit=Command.of(submit_argv(rc, p["task_id"])),
        ))
    return out


def dispatch_why(n: int, stage: str, kinds: list[str]) -> str:
    return (f"{n} independent {'/'.join(kinds) or stage} task(s) need results. Each task is self-contained: run "
            f"them as parallel subagents up to the concurrency, or one after another inline in this session when "
            f"subagents are not available; the prompt and submit command are the same either way. Write each "
            f"result to its result_path and run its submit command.")


# --- submit ------------------------------------------------------------------------


def validate_submission(rc: "RunContext", plan: StagePlan, pack: dict[str, Any], payload: Any) -> Any:
    from docket.tasks.submit import validate
    kind = plan.kinds[pack["kind"]]
    texts = RecordTexts(rc, {str(it.get("source")) for it in pack["items"]})
    rerun = Command.of(submit_argv(rc, pack["task_id"])).shell
    return validate(kind, pack, payload, raw_text=lambda iid: texts.for_records(plan.records.get(iid, [])),
                    rerun=rerun)


# --- combine -------------------------------------------------------------------------


def _rows(plan: StagePlan) -> dict[str, dict[str, tuple[str, dict[str, Any]]]]:
    """kind -> item id -> (task id, row)."""
    out: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
    for p in plan.packs:
        res = plan.results.get(p["task_id"])
        if res is None:
            continue
        bucket = out.setdefault(p["kind"], {})
        for row in res.get("items", []):
            bucket[row["id"]] = (p["task_id"], row)
    return out


def _trace(it: dict[str, Any], entry: dict[str, Any]) -> list[Any]:
    tr = it.get("trace")
    return [*(tr if isinstance(tr, list) else []), entry]


def _body(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "id"}


def _extra_kinds(new: dict[str, Any], rows: dict[str, dict[str, tuple[str, dict[str, Any]]]], plan: StagePlan,
                 iid: str) -> None:
    for kname, bucket in sorted(rows.items()):
        if kname == plan.stage or iid not in bucket:
            continue
        _, row = bucket[iid]
        if kname == "extract":
            new["extracted"] = list(row.get("entries") or [])
        elif kname == "classify":
            new["classified"] = _body(row)
        else:
            new["tasks"] = {**(new.get("tasks") if isinstance(new.get("tasks"), dict) else {}), kname: _body(row)}


def combine_assess(items: list[dict[str, Any]], plan: StagePlan) -> dict[str, Any]:
    rows = _rows(plan)
    assess = rows.get("assess", {})
    out = []
    for it in items:
        new = dict(it)
        hit = assess.get(it["id"])
        row = hit[1] if hit else {}
        new["keep"] = bool(row.get("keep"))
        new["score"] = int(row.get("score") or 0)
        if hit:
            new["assessment"] = _body(row)
            new["trace"] = _trace(it, {"stage": "assess", "task": hit[0], "keep": new["keep"],
                                       "score": new["score"], "kind": row.get("kind")})
        _extra_kinds(new, rows, plan, it["id"])
        out.append(new)
    return {"items": out}


def combine_review(items: list[dict[str, Any]], plan: StagePlan) -> dict[str, Any]:
    rows = _rows(plan)
    verdicts: dict[str, tuple[str | None, dict[str, Any]]] = {k: (t, r) for k, (t, r) in rows.get("review", {}).items()}
    for iid, row in plan.carried.items():
        verdicts.setdefault(iid, (None, row))
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
        _extra_kinds(new, rows, plan, iid)
        hit = verdicts.get(iid)
        if hit is None:
            out.append(new)
            continue
        task, row = hit
        v = row.get("verdict")
        review = {k: row[k] for k in ("against", "reason", "merge_with") if row.get(k)}
        entry: dict[str, Any] = {"stage": "review", "verdict": v, **review}
        if task is None:
            entry["carried"] = True
        else:
            entry["task"] = task
        new["verdict"] = v
        new["review"] = review
        if v == "drop":
            new["keep"] = False
            new["dropped"] = {"stage": "review", "reason": row.get("reason") or ""}
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


def kind_names(rc: "RunContext") -> list[str]:
    return sorted(kinds_for(rc.ws.config))
