"""Items and configuration testing (TD §8.4, §13.8, §15.1; FR-SIG-09, CFG-07):
``item why <ref>``, ``item list [--filter]`` and ``config test``, which are read-only, and
``item add <text>``, which writes one task line and its ledger row outside any run."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError, StateError
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
    merged = rc.of("merge")
    if merged is not None and merged.output:
        return merged.output
    nodes = rc.by_verb("merge")
    dg, data = stored_merge(rc.run, nodes[0] if nodes else None)
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
    from docket.signals.explain import explain, find, later_nodes
    rc = _run_context(ctx)
    data = _merge_data(ctx, rc)
    top, mem = find(data, ns.ref)
    out = explain(top, mem, later_nodes(rc, top["id"]) if mem is None else [])
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


def _add_argv(ns: Any, root: Any, text: str | None = None) -> list[str]:
    """The ``item add`` command line as given, for a fix hint."""
    argv = ["docket", "item", "add", ns.text if text is None else text]
    for link in ns.link or []:
        argv += ["--link", link]
    for flag, v in (("--label", ns.label if ns.label != "link" else None), ("--due", ns.due), ("--est", ns.est),
                    ("--area", ns.area), ("--tier", ns.tier), ("--note", ns.note), ("--to", ns.to)):
        if v is not None:
            argv += [flag, v]
    return argv + ["--workspace", str(root)]


@command("item add", "Add a task the operator has decided on, with no question asked. The destination renders "
                     "the line as it renders a triaged item's, quality rules included, and writes it; the ledger "
                     "enters the item in backlog. Needs no run. Only a private task store takes it.",
         args=[Arg("text", "What to do, in the imperative: the task's action."),
               Arg("--link", "A link to keep with the task (repeatable). The first is the item's URL.",
                   action="append", metavar="URL"),
               Arg("--label", "What a template calls the link (source_label). Default: link.", default="link",
                   metavar="TEXT"),
               Arg("--due", "Due date: YYYY-MM-DD, today, tomorrow or a weekday name.", metavar="DATE"),
               Arg("--est", "Estimate: 45, 45m, 1h, 1.5h or 1h30m.", metavar="DURATION"),
               Arg("--area", "The task's area.", metavar="TEXT"),
               Arg("--tier", "The tier whose section takes the line, one of the destination's sections.",
                   metavar="NAME"),
               Arg("--note", "Why, or where it came from: the task's evidence, kept in the ledger history.",
                   metavar="TEXT"),
               Arg("--to", "The task store. Default: the workspace's only destination that accepts task.add.",
                   metavar="DESTINATION"),
               Arg("--now", "Take today from this ISO instant instead of the wall clock (tests).")],
         mutating=True, effects=["appends one line to a private task-store destination",
                                 "writes one ledger row and a history entry made by hand"],
         examples=["docket item add 'Watch the storage talk' --link https://video.example.org/v/1 --est 45m --json",
                   "docket item add 'Call the dentist' --due fri --tier priority --dry-run --json"])
def cmd_item_add(ctx: Any, ns: Any) -> Result:
    import shlex
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from docket.items.add import add
    from docket.text.dates import normalize_date
    from docket.text.durations import format_duration, parse_duration
    ws = ctx.workspace()
    if ns.now:
        try:
            now = datetime.fromisoformat(ns.now)
        except ValueError:
            raise InputError("input.bad_argument", f"--now must be an ISO instant, not {ns.now!r}.",
                             hint="docket item add <text> --now 2026-10-15T09:00:00+00:00") from None
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(ws.config.profile.timezone)).date()
    due = normalize_date(ns.due, today) if ns.due is not None else None
    est = parse_duration(ns.est) if ns.est is not None else None
    root = ctx.workspace_root()

    def fix(flag: str, placeholder: str) -> str | None:
        if flag == "--title":
            return shlex.join(_add_argv(ns, root, placeholder))
        if flag in ("--due", "--est", "--note", "--link"):
            return shlex.join(_add_argv(ns, root) + [flag, placeholder])
        return None

    links = [x.strip() for x in ns.link or [] if x.strip()]
    area = ns.area.strip() if ns.area and ns.area.strip() else None
    dry = ctx.dry_run
    db = ctx.store().db
    with db:
        out = add(ws, db, text=ns.text, links=links, label=ns.label, due=due, est=est, area=area, tier=ns.tier,
                  note=ns.note, to=ns.to, today=today.isoformat(), at=now.astimezone(timezone.utc).isoformat(),
                  env=ctx.env, fix=fix, write=not dry)
    where = f"{out['destination']}: {out['path']}" + (f" under {out['section']}" if out["section"] else "")
    if not out["changed"]:
        text = [f"{out['key']} is already open in {where}; nothing written", f"  {out['line']}"]
        return Result(out, "\n".join(text))
    text = [f"{'would add' if dry else 'added'} {out['key']} to {where}", f"  {out['line']}"]
    row = out["ledger"]
    facts = [f"due {row['due']}" if row.get("due") else "", f"est {format_duration(row['est'])}" if row.get("est") else "",
             f"area {row['area']}" if row.get("area") else ""]
    text.append(f"  ledger: {row['key']} {row['state']}" + "".join(f", {f}" for f in facts if f))
    return Result(out, "\n".join(text))


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
        from docket.sources.records import index_paths
        for row in store.list_runs():
            cand = load_run(store, row["run_id"])
            if index_paths(cand.dir):
                run = cand
                break
    if run is None:
        raise StateError("run.no_records", "No run of this workspace has read records to test against.",
                         hint="docket run start <graph>, then docket do <read node>")
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
