"""``docket seal``: the seal steps in order, each receipted (TD §13.4 to §13.7, FR-APP-06).

1. ``seal:state``, one transaction: apply cursor proposals, write
   dispositions, append configured ledgers, and the step receipt. A sandbox
   run writes none of the three durable records (only the receipt); its journal
   still lists the dispositions it would have written.
2. ``seal:journal``: render the journal, write ``journal.md`` in the run
   directory and, when ``journal.destination`` names a destination, write the
   entry through it as an operation with an apply receipt.

The engine then writes the seal artifact and marks the run sealed. A seal that
stops anywhere starts again at the first step without a receipt.
"""
from __future__ import annotations

from typing import Any

from docket.seal import cursors as seal_cursors
from docket.seal import dispositions as disp_mod
from docket.seal import journal as journal_mod
from docket.seal import ledgers, receipts
from docket.items import adopt as item_adopt
from docket.items import ledger as item_ledger
from docket.store.paths import write_atomic


def _decided(rc: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, list[dict[str, Any]],
                               dict[str, dict[str, Any]]]:
    """What the run decided and wrote: the operator's decisions, the outcomes of decisions
    that wrote nothing, every operation approved or written without approval, and the
    receipts by operation."""
    dec, plan, appr = rc.of("ask"), rc.of("plan"), rc.of("approve")
    decide = dec.output if dec is not None else None
    outcomes = None
    if plan is not None and plan.output:
        outcomes = plan.output.get("outcomes")
        if outcomes is None:
            outcomes = journal_mod.plan_document(rc, plan.output.get("plan_digest")).get("outcomes")
    wrote = rc.output("write")
    ops = list(((appr.output or {}).get("operations") if appr else None) or []) + list(wrote.get("operations") or [])
    recs = {x["op_id"]: x for x in [*(rc.output("apply").get("receipts") or []), *(wrote.get("receipts") or [])]}
    return decide, outcomes, ops, recs


def _standing_blocks(rc: Any, ops: list[dict[str, Any]], recs: dict[str, dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The calendar blocks that stand after this run, for the item ledger: those the plan
    kept, and those apply wrote. None when the run planned or applied nothing, which says
    nothing about what stands."""
    from docket.plan.build import planning_active
    if not planning_active(rc) or rc.of("approve") is None or rc.of("apply") is None:
        return None
    cal = rc.ws.config.planning.calendar
    out = []
    for o in ops:
        if o.get("destination") != cal.destination or o.get("verb") not in ("create", "move", "update", "keep"):
            continue
        rec = recs.get(o.get("op_id", ""))
        if o["verb"] != "keep" and (rec is None or rec.get("outcome") not in ("applied", "already_present")):
            continue
        if o.get("task_ids"):
            out.append({"key": o.get("key"), "task_ids": list(o["task_ids"]), "start": o.get("start"), "end": o.get("end")})
    return out


def durable_texts(rc: Any) -> dict[str, str]:
    """The words each labelled task had when its label was written: the label nodes' packs."""
    out: dict[str, str] = {}
    for name in rc.judging("label"):
        for p in (rc.eval(name).extra or {}).get("packs") or []:
            for it in p.get("items") or []:
                out[it["id"]] = "\n".join(ln["text"] for ln in it.get("lines") or [])
    return out


def _ledger_rows(rc: Any, decide: dict[str, Any] | None, ops: list[dict[str, Any]],
                 dispositions: list[Any]) -> list[dict[str, Any]]:
    """The run's rows for the item ledger: every item merge kept, with the decision made on it."""
    kept = [it for it in rc.output("merge").get("items") or [] if it.get("status") == "kept"]
    decide = decide or {}
    by_topic_q = {str(f["item"].get("topic") or f["item"].get("id")): f["question_id"]
                  for f in decide.get("findings") or []}
    questions = {q["id"]: q for q in decide.get("questions") or []}
    answers = decide.get("answers") or {}
    op_by_id = {o.get("op_id"): o for o in ops}
    decided = []
    for d in dispositions:
        row: dict[str, Any] = {"topic": d.topic_id, "decision": d.decision, "note": d.note}
        qid = by_topic_q.get(str(d.topic_id))
        ov = (answers.get(qid) or {}).get("overrides") or {}
        dflt = (questions.get(qid) or {}).get("defaults") or {}
        for k in ("due", "est", "area"):
            v = ov.get(k) if ov.get(k) not in (None, "") else dflt.get(k)
            if v not in (None, ""):
                row[k] = v
        if d.destination:
            cfg = rc.ws.config.destinations.get(d.destination)
            op = op_by_id.get(d.op_id) or {}
            row.update({"destination": d.destination, "adapter": cfg.adapter if cfg is not None else None,
                        "op_id": d.op_id, "path": op.get("path"), "anchor": op.get("anchor"), "key": op.get("key"),
                        "line": op.get("line")})
        decided.append(row)
    return item_ledger.derive(rc.run.id, rc.run.header.clock.instant, kept=kept, decided=decided)


def destination_for(rc: Any) -> str | None:
    name = rc.ws.config.journal.destination
    if name and name in rc.ws.config.destinations and rc.ws.config.destinations[name].adapter == "journal":
        return name
    return None


def state_step(rc: Any) -> dict[str, Any]:
    r = rc.run
    db = r.store.db
    prior = receipts.done(db, r.id, "seal:state")
    if prior is not None:
        return prior
    decide, outcomes, ops, recs = _decided(rc)
    rows = disp_mod.derive(r.id, decide, outcomes, ops, recs)
    specs = rc.ws.config.journal.ledgers
    ctx = journal_mod.build_context(rc, dispositions=[d.row() for d in rows])
    entries = ledgers.compute(specs, ctx)
    durable = not r.header.sandbox  # a sandbox run leaves cursors, dispositions and ledgers as they were
    items = _ledger_rows(rc, decide, ops, rows)
    observed: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    adopted: list[dict[str, Any]] = []
    labels_now = dict(rc.output("judge").get("labels") or {})
    label_texts = durable_texts(rc) if labels_now else {}
    with db:
        moved = seal_cursors.advance(db, r)
        if durable:
            disp_mod.write(db, rows)
            item_ledger.write(db, items)
            # task lines adopted by this run enter the ledger before its blocks are tracked to them
            adopted = item_adopt.enter(db, ops, recs, run_id=r.id, at=r.header.clock.instant)
            standing = _standing_blocks(rc, ops, recs)
            if standing is not None:
                cal = rc.ws.config.planning.calendar
                dcfg = rc.ws.config.destinations.get(cal.destination)
                planned = item_ledger.place_blocks(db, standing, destination=cal.destination,
                                                   adapter=dcfg.adapter if dcfg is not None else "calendar",
                                                   run_id=r.id, at=r.header.clock.instant)
            # agendas rolled by this run: the raised items the meetings covered are done
            rolled = [o for o in ops if o.get("verb") == "roll"
                      and (recs.get(o.get("op_id", "")) or {}).get("outcome") in ("applied", "already_present")]
            if rolled:
                covered = item_ledger.cover(db, rolled, run_id=r.id, at=r.header.clock.instant)
            if labels_now:
                from docket.plan import labels as plan_labels
                plan_labels.remember(db, labels_now, label_texts, run_id=r.id,
                                     open_ids=rc.output("judge").get("open") or [])
            # after the run's own writes: follow what the operator did in the destinations
            observed = item_ledger.observe(db, item_ledger.read_back(rc.ws, run=r, env=rc.env), run_id=r.id,
                                           at=r.header.clock.instant)
            for name, key, row in entries:
                ledgers.append(db, name, r.id, key, row)
        body = {"cursors": moved, "dispositions": [d.row() for d in rows], "durable": durable,
                "items": len(items) if durable else 0, "items_adopted": len(adopted), "items_observed": len(observed), "items_planned": len(planned), "items_covered": len(covered),
                "ledgers": {n: sum(1 for e in entries if e[0] == n) if durable else 0 for n in sorted(specs)}}
        receipts.put(db, r.id, "seal:state", "ok", body)
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
    prior = receipts.done(db, r.id, "seal:journal")
    if prior is not None:
        return prior
    if dest is not None:
        out.update(entry_through_destination(rc, dest, cfg, ctx))
    with db:
        receipts.put(db, r.id, "seal:journal", "ok", out)
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


def execute_seal(rc: Any) -> dict[str, Any]:
    from docket.engine.next import unwritten as accepted_unwritten
    state = state_step(rc)
    jr = journal_step(rc, state)
    cur = state.get("cursors") or {}
    return {"cursors_advanced": cur.get("advanced", []), "cursors_held": cur.get("held", []),
            "cursor_regressions": cur.get("regressions", []), "cursors_skipped": cur.get("skipped"),
            "dispositions": len(state.get("dispositions") or []), "ledgers": state.get("ledgers") or {},
            "journal": jr.get("path", "journal.md"), "journal_destination": jr.get("destination"),
            "journal_write": jr.get("write"), "unwritten": accepted_unwritten(rc),
            "sealed": True, "sandbox": rc.run.header.sandbox}


def preview_seal(rc: Any) -> dict[str, Any]:
    """``docket seal --dry-run``: what sealing would do."""
    db = rc.run.store.db
    done = {s: receipts.done(db, rc.run.id, s) is not None for s in ("seal:state", "seal:journal")}
    props = [dict(p) for p in db.execute("SELECT source, unit, position FROM cursor_proposals WHERE run_id=? "
                                         "ORDER BY source, unit", (rc.run.id,))]
    if rc.run.header.sandbox:
        props = []
    return {"steps_done": done, "cursor_proposals": len(props), "proposals": props[:50],
            "journal_destination": destination_for(rc)}
