"""The run journal (TD §13.5).

``build_context`` assembles the closed journal context from the run's current
artifacts; ``docket.config.contexts.JOURNAL`` declares every field, and
validation rejects a template naming any other. ``render`` turns the
configured sections (or the built-in layout) into Markdown. The context is a
pure function of the run's artifacts, receipts and database rows, so a resumed
close renders the same journal and its journal operation keeps its id.

Context lists: ``sources`` (outcomes and coverage), ``findings``, ``questions``,
``answers``, ``operations`` (with receipt outcomes), ``not_asked``, ``dropped``
(dropped and suppressed topics with reasons), ``coverage_notes``, ``context``
(context records), ``unexpected``, ``dispositions``; maps ``run``, ``cursors``,
``counts``, ``ledgers`` (rows by ledger across recent runs) and ``approval``.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from docket.text import templates

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


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _unexpected_rows(run: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    p = run.paths.unexpected
    if not p.is_file():
        return rows
    seen = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        row = json.loads(ln)
        key = (row["area"], row["detail"])
        if key not in seen:
            seen.add(key)
            rows.append({k: row.get(k) for k in ("area", "detail", "reading", "extend")})
    return rows


def plan_document(rc: Any, plan_digest: str | None) -> dict[str, Any]:
    if not plan_digest:
        return {}
    p = rc.run.paths.plan(plan_digest) / "plan.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def approval_view(rc: Any, plan_digest: str | None) -> dict[str, Any]:
    """The approval record of this plan from the ``approvals`` ledger, with the delay since the build."""
    from docket.close.ledgers import rows
    out: dict[str, Any] = {"digest": plan_digest or "", "delay": "", "delay_minutes": "", "approved_at": "",
                           "built_at": ""}
    if not plan_digest:
        return out
    hits = [r for r in rows(rc.run.store.db, "approvals") if r.get("run") == rc.run.id
            and r.get("plan_digest") == plan_digest]
    if not hits:
        return out
    rec = hits[-1]
    out.update(approved_at=_s(rec.get("approved_at")), built_at=_s(rec.get("built_at")))
    try:
        a, b = datetime.fromisoformat(out["approved_at"]), datetime.fromisoformat(out["built_at"])
    except ValueError:
        return out
    mins = max(0, int((a - b).total_seconds() // 60))
    out["delay_minutes"] = mins
    out["delay"] = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    return out


def build_context(rc: Any, *, cursors: dict[str, Any] | None = None,
                  dispositions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    r = rc.run
    hdr = r.header
    col = rc.eval_or_none("collect")
    mrg = rc.eval_or_none("merge")
    dec = rc.eval_or_none("decide")
    appr = rc.eval_or_none("approve")
    app = rc.eval_or_none("apply")
    col_out = (col.output or {}) if col else {}
    sources = []
    for sid, row in sorted((col_out.get("sources") or {}).items()):
        sources.append({"id": sid, "outcome": row.get("outcome"), "records": row.get("records", 0),
                        "reason": row.get("reason") or "", "coverage_end": row.get("coverage_end") or "",
                        "generations": ",".join(str(g) for g in row.get("generations") or [])})
    findings, questions, answers, not_asked = [], [], [], []
    dec_out = (dec.output or {}) if dec else {}
    items = {f["question_id"]: f["item"] for f in dec_out.get("findings") or []}
    for qid, it in items.items():
        findings.append({"question_id": qid, "item_id": it.get("id"), "title": it.get("title", ""),
                         "source": it.get("source"), "topic": it.get("topic") or it.get("id"),
                         "score": it.get("score"), "url": it.get("url") or "", "from": it.get("from") or "",
                         "reason": it.get("reason") or ""})
    for q in dec_out.get("questions") or []:
        questions.append({"id": q["id"], "kind": q.get("kind"), "prompt": q.get("prompt", ""),
                          "item_id": q.get("item_id")})
    for qid, a in sorted((dec_out.get("answers") or {}).items()):
        it = items.get(qid) or {}
        ov = a.get("overrides") or {}
        answers.append({"question_id": qid, "option": a.get("option"), "action": a.get("action"),
                        "title": it.get("title", ""), "item_id": it.get("id"), "topic": it.get("topic") or it.get("id"),
                        "note": _s(ov.get("note")), "due": _s(ov.get("due")), "est": _s(ov.get("est")),
                        "when": _s(ov.get("when")), "until": _s(ov.get("until"))})
    for x in dec_out.get("not_asked") or []:
        not_asked.append({"item_id": x.get("item_id"), "title": x.get("title", ""), "score": x.get("score"),
                          "source": x.get("source")})
    ops = []
    if app is not None and app.output:
        recs = {x["op_id"]: x for x in app.output.get("receipts") or []}
        for o in ((appr.output or {}).get("operations") if appr else None) or []:
            rec = recs.get(o["op_id"]) or {}
            err = rec.get("error") or {}
            ops.append({**o, "outcome": rec.get("outcome", "pending"), "verify_status": rec.get("verify_status", ""),
                        "line": o.get("line") or o.get("title") or o.get("subject") or "",
                        "error_code": err.get("code", ""), "error_message": err.get("message", "")})
    mrg_out = (mrg.output or {}) if mrg else {}
    dropped = [{"item_id": it.get("id"), "title": it.get("title", ""), "source": it.get("source"),
                "status": it.get("status"), "reason": it.get("reason") or "", "topic": it.get("topic")}
               for it in mrg_out.get("items") or [] if it.get("status") in ("dropped", "suppressed")]
    coverage = [{k: n.get(k) for k in ("note", "home", "value", "item_id", "source", "title", "home_outcome")}
                for n in mrg_out.get("coverage_notes") or []]
    context = [{k: c.get(k) for k in ("id", "source", "title", "text", "at", "url")}
               for c in col_out.get("context") or []]
    cur = cursors or {}
    disp = [{"topic": d["topic_id"], "decision": d["decision"], "item_id": d.get("item"),
             "destination": d.get("destination"), "op_id": d.get("op_id"), "until": d.get("until"),
             "note": d.get("note"), "outcome": d.get("outcome")} for d in dispositions or []]
    unexpected_rows = _unexpected_rows(r)
    counts = {"sources": len(sources), "findings": len(findings), "answers": len(answers),
              "operations": len(ops), "applied": sum(1 for o in ops if o["outcome"] == "applied"),
              "failed": sum(1 for o in ops if o["outcome"] in ("failed", "refused", "blocked")),
              "not_asked": len(not_asked), "dropped": len(dropped), "coverage_notes": len(coverage),
              "unexpected": len(unexpected_rows), "dispositions": len(disp)}
    plan_digest = (appr.output or {}).get("plan_digest") if appr else None
    return {
        "run": {"id": r.id, "date": hdr.clock.date, "workflow": hdr.workflow, "sandbox": hdr.sandbox,
                "timezone": hdr.clock.timezone, "instant": hdr.clock.instant},
        "sources": sources, "findings": findings, "questions": questions, "answers": answers,
        "operations": ops, "not_asked": not_asked, "dropped": dropped, "coverage_notes": coverage,
        "context": context, "unexpected": unexpected_rows, "dispositions": disp,
        "cursors": {"advanced": list(cur.get("advanced") or []), "held": list(cur.get("held") or []),
                    "regressions": list(cur.get("regressions") or [])},
        "counts": counts, "ledgers": {}, "approval": approval_view(rc, plan_digest),
    }


def render(cfg: Any, ctx: dict[str, Any], *, level: int = 2, title: bool = True) -> str:
    """The journal text. ``level`` is the heading level of sections; ``title`` adds the level-1 title."""
    if not cfg.sections:
        text = templates.render(JOURNAL_TEMPLATE, ctx)
        if level != 2 or not title:
            lines = text.split("\n")
            if not title:
                lines = lines[2:] if lines and lines[0].startswith("# ") else lines
            text = "\n".join(("#" * level + ln[2:]) if ln.startswith("## ") else ln for ln in lines)
        out = text.rstrip("\n") + "\n"
    else:
        parts = []
        if title:
            parts.append("# " + templates.render(cfg.title, ctx).strip())
        for sec in cfg.sections:
            body = templates.render(sec.template, ctx).rstrip()
            if sec.skip_empty and not body.strip():
                continue
            parts.append(f"{'#' * level} {templates.render(sec.heading, ctx).strip()}\n{body}".rstrip())
        out = "\n\n".join(parts) + "\n"
    if cfg.approval_delay_note and ctx.get("approval", {}).get("delay") != "":
        note = templates.render(cfg.approval_delay_note, ctx).strip()
        if note:
            out = out.rstrip("\n") + "\n\n" + note + "\n"
    return out
