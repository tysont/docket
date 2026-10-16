"""Item inspection and configuration testing (TD §8.4, §15.1; FR-SIG-09, CFG-07):
``item why <ref>``, ``item list [--filter]`` and ``config test``. All are read-only."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import StateError
from docket.cli.registry import Arg, command


def _run_context(ctx: Any) -> Any:
    """The named run, else the latest open run, else the most recent run (sealed runs stay inspectable)."""
    try:
        return ctx.run_context()
    except StateError as e:
        if e.code != "run.none_open":
            raise
        rows = ctx.store().list_runs()
        if not rows:
            raise
        return ctx.run_context(rows[0]["run_id"])


def _merge_data(ctx: Any, rc: Any) -> dict[str, Any]:
    from docket.signals.replay import stored_merge
    if "merge" in rc.workflow.stages:
        ev = rc.eval("merge")
        if ev.state == "current" and ev.output:
            return ev.output
    dg, data = stored_merge(rc.run)
    if data is None:
        nxt = rc.argv("next")
        raise StateError("item.not_merged", f"Run {rc.run.id} has no merge artifact yet.", hint=" ".join(nxt),
                         next_command=nxt)
    ctx.warn("item.stale_merge", f"The merge artifact {dg[:12]} is not current; showing it as built.")
    return data


@command("item why", "Explain how an item was kept, scored, merged, suppressed or dropped, with rule ids and "
                     "their file positions.",
         run=True, args=[Arg("ref", "Item id, topic id, URL, or words of the item's title or text.")],
         examples=["docket item why 3f9c0a1b2d4e5f60 --json", "docket item why 'capacity doc' --json"])
def cmd_item_why(ctx: Any, ns: Any) -> Result:
    from docket.signals.explain import explain, find, later_stages
    rc = _run_context(ctx)
    data = _merge_data(ctx, rc)
    top, mem = find(data, ns.ref)
    out = explain(top, mem, later_stages(rc, top["id"]) if mem is None else [])
    head = f"{out['id']} [{out['source']}] {out['title'][:90]}\nstatus {out['status']}, score {out['score']}" + \
        (f", reason: {out['reason']}" if out["reason"] else "") + \
        (f"\nmerged into {out['topic_item']} (topic {out['topic']})" if out["merged"] else f"\ntopic {out['topic']}")
    return Result({"item": out}, head + "\n" + "\n".join("  " + ln for ln in out["lines"]))


@command("item list", "List the run's merged items with status, score and reason.",
         run=True, args=[Arg("--filter", "key=value pairs (status, source, unit, topic, tag, surface, sensitivity, "
                                         "route, rule, keep) and words of the title or text.", metavar="EXPR")],
         examples=["docket item list --json", "docket item list --filter status=kept,source=mail --json",
                   "docket item list --filter 'status=dropped latency' --json"])
def cmd_item_list(ctx: Any, ns: Any) -> Result:
    from docket.signals.explain import matches, parse_filter
    rc = _run_context(ctx)
    data = _merge_data(ctx, rc)
    filters, words = parse_filter(ns.filter)
    rows = []
    for it in data.get("items") or []:
        if not matches(it, filters, words):
            continue
        sig = it.get("signals") or {}
        rows.append({"id": it["id"], "topic": it.get("topic"), "source": it.get("source"),
                     "title": (it.get("title") or "")[:120], "status": it.get("status"), "keep": it.get("keep"),
                     "score": it.get("score"), "reason": it.get("reason"), "surface": sig.get("surface"),
                     "route": sig.get("route"), "sensitivity": sig.get("sensitivity"), "tags": sig.get("tags"),
                     "rules": sig.get("rules"), "members": [m["id"] for m in it.get("members") or []]})
    text = "\n".join(f"{r['id']} {r['status']:<10} {r['score']:>3} [{r['source']}] {r['title'][:70]}" for r in rows)
    return Result({"items": rows, "counts": data.get("counts"), "coverage_notes": data.get("coverage_notes") or []},
                  text or "no items match")


@command("config test", "Rerun mapping, rules, scoring and merge on the last run's raw records under the current "
                        "configuration and show what changed. Writes nothing.",
         run=True, examples=["docket config test --json", "docket config test --run 2026-10-16-daily --json"])
def cmd_config_test(ctx: Any, ns: Any) -> Result:
    from docket.config import check, invalid_error
    from docket.signals.replay import config_test
    from docket.store.runs import load_run
    ws, issues, _ = check(ctx.workspace_root())
    if ws is None:
        raise invalid_error(issues)
    store = ctx.store()
    run = None
    if getattr(ns, "run", None):
        run = load_run(store, ns.run)
    else:
        for row in store.list_runs():
            cand = load_run(store, row["run_id"])
            if any(cand.dir.glob("gen-*/records/*.jsonl")):
                run = cand
                break
    if run is None:
        raise StateError("run.no_records", "No run of this workspace has collected records to test against.",
                         hint="docket run start <workflow>, then docket collect")
    ctx.run_id = run.id
    out = config_test(ws, store, run)
    d = out["diff"]
    lines = [f"config test against {run.id}: {out['counts']['kept']} kept of {out['counts']['topics']} topic(s)"
             + ("" if out["compared"] else " (no stored merge to compare)")]
    lines += [f"+ kept {x['id']} {x['score']} {x['title'][:70]}" for x in d["kept_added"]]
    lines += [f"- dropped {x['id']} {x['score']} {x['title'][:70]}" for x in d["kept_removed"]]
    lines += [f"~ score {x['id']} {x['before']} -> {x['after']} {x['title'][:60]}" for x in d["score_changes"]]
    lines += [f"~ route {x['id']} {x['before']} -> {x['after']}" for x in d["route_changes"]]
    if not d["changed"]:
        lines.append("no changes")
    return Result(out, "\n".join(lines))
