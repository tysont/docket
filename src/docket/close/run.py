"""``docket close``: the close steps in order, each receipted (TD §13.4 to §13.7, FR-APP-06).

1. ``close:state``, one transaction: apply cursor proposals, write
   dispositions, append configured ledgers, and the step receipt. A sandbox
   run writes none of the three durable records (only the receipt); its journal
   still lists the dispositions it would have written.
2. ``close:journal``: render the journal, write ``journal.md`` in the run
   directory and, when ``journal.destination`` (or the workflow's ``journal``)
   names a destination, write the entry through it as an operation with an
   apply receipt.
3. ``after_close`` hooks, each with its own receipt.

The engine then writes the close artifact and seals the run. A close that
stops anywhere resumes at the first step without a receipt.
"""
from __future__ import annotations

from typing import Any

from docket.close import cursors as close_cursors
from docket.close import dispositions as disp_mod
from docket.close import journal as journal_mod
from docket.close import ledgers, receipts
from docket.close.hooks import run_event
from docket.store.paths import write_atomic


def _decided(rc: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, list[dict[str, Any]],
                               dict[str, dict[str, Any]]]:
    dec = rc.eval_or_none("decide")
    plan = rc.eval_or_none("plan")
    appr = rc.eval_or_none("approve")
    app = rc.eval_or_none("apply")
    decide = dec.output if dec is not None else None
    outcomes = None
    if plan is not None and plan.output:
        outcomes = plan.output.get("outcomes")
        if outcomes is None:
            outcomes = journal_mod.plan_document(rc, plan.output.get("plan_digest")).get("outcomes")
    ops = list(((appr.output or {}).get("operations") if appr else None) or [])
    recs = {x["op_id"]: x for x in ((app.output or {}).get("receipts") if app else None) or []}
    return decide, outcomes, ops, recs


def destination_for(rc: Any) -> str | None:
    name = rc.workflow.journal or rc.ws.config.journal.destination
    if name and name in rc.ws.config.destinations and rc.ws.config.destinations[name].adapter == "journal":
        return name
    return None


def state_step(rc: Any) -> dict[str, Any]:
    r = rc.run
    db = r.store.db
    prior = receipts.done(db, r.id, "close:state")
    if prior is not None:
        return prior
    decide, outcomes, ops, recs = _decided(rc)
    rows = disp_mod.derive(r.id, decide, outcomes, ops, recs)
    specs = rc.ws.config.journal.ledgers
    ctx = journal_mod.build_context(rc, dispositions=[d.row() for d in rows])
    entries = ledgers.compute(specs, ctx)
    durable = not r.header.sandbox  # a sandbox run leaves cursors, dispositions and ledgers as they were
    with db:
        moved = close_cursors.advance(db, r)
        if durable:
            disp_mod.write(db, rows)
            for name, key, row in entries:
                ledgers.append(db, name, r.id, key, row)
        body = {"cursors": moved, "dispositions": [d.row() for d in rows], "durable": durable,
                "ledgers": {n: sum(1 for e in entries if e[0] == n) if durable else 0 for n in sorted(specs)}}
        receipts.put(db, r.id, "close:state", "ok", body)
    return {**body, "outcome": "ok"}


def journal_step(rc: Any, state: dict[str, Any]) -> dict[str, Any]:
    r = rc.run
    db = r.store.db
    cfg = rc.ws.config.journal
    ctx = journal_mod.build_context(rc, cursors=state.get("cursors"), dispositions=state.get("dispositions"))
    ctx["ledgers"] = ledgers.context(db, cfg.ledgers)
    text = journal_mod.render(cfg, ctx)
    write_atomic(r.paths.journal, text)
    out: dict[str, Any] = {"path": "journal.md", "destination": None}
    dest = destination_for(rc)
    prior = receipts.done(db, r.id, "close:journal")
    if prior is not None:
        return prior
    if dest is not None:
        out.update(entry_through_destination(rc, dest, cfg, ctx))
    with db:
        receipts.put(db, r.id, "close:journal", "ok", out)
    return {**out, "outcome": "ok"}


def entry_through_destination(rc: Any, dest_id: str, cfg: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from docket.apply import executor
    from docket.destinations import for_run
    from docket.destinations.base import Action
    from docket.destinations.markdown_io import heading_level
    from docket.text import templates
    d = for_run(rc, dest_id)
    try:
        heading_tpl = rc.ws.config.destinations[dest_id].section or "## {run.date} {run.id}"
        heading = templates.render(heading_tpl, ctx).strip()
        level = min(6, (heading_level(heading) or 1) + 1)
        body = journal_mod.render(cfg, ctx, level=level, title=False)
        [op] = d.preview(Action(kind="journal.write", destination=dest_id,
                                fields={"run": ctx["run"], "heading": heading, "body": body}), d.snapshot())
    finally:
        d.close()
    [row] = executor.execute(rc, [op.dump()])
    return {"destination": dest_id, "op_id": row["op_id"], "write": row["outcome"],
            "verify_status": row.get("verify_status"), "error": row.get("error")}


def execute_close(rc: Any) -> dict[str, Any]:
    state = state_step(rc)
    jr = journal_step(rc, state)
    hooks = run_event(rc, "after_close")
    cur = state.get("cursors") or {}
    return {"cursors_advanced": cur.get("advanced", []), "cursors_held": cur.get("held", []),
            "cursor_regressions": cur.get("regressions", []), "cursors_skipped": cur.get("skipped"),
            "dispositions": len(state.get("dispositions") or []), "ledgers": state.get("ledgers") or {},
            "journal": jr.get("path", "journal.md"), "journal_destination": jr.get("destination"),
            "journal_write": jr.get("write"), "hooks": [h.brief() for h in hooks],
            "sealed": True, "sandbox": rc.run.header.sandbox}


def preview_close(rc: Any) -> dict[str, Any]:
    """``docket close --dry-run``: what close would do; runs only the hooks' declared dry-run forms."""
    db = rc.run.store.db
    done = {s: receipts.done(db, rc.run.id, s) is not None for s in ("close:state", "close:journal")}
    props = [dict(p) for p in db.execute("SELECT source, unit, position FROM cursor_proposals WHERE run_id=? "
                                         "ORDER BY source, unit", (rc.run.id,))]
    if rc.run.header.sandbox:
        props = []
    hooks = run_event(rc, "after_close", dry_run=True)
    return {"steps_done": done, "cursor_proposals": len(props), "proposals": props[:50],
            "journal_destination": destination_for(rc), "hooks": [h.brief() for h in hooks]}
