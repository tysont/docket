"""From answers to plan operations (TD §10.3, §12.6): the mapping the plan stage uses.

Every answer kind becomes operations previewed by its destination adapter, or
a non-destination outcome:

| question / action      | result                                                                  |
|------------------------|-------------------------------------------------------------------------|
| finding ``task.add``   | ``append`` to the task store, with every override in the line context    |
| finding ``agenda.add`` | ``append`` to the forum's agenda (``--forum`` or the derived forum)      |
| finding ``calendar.place`` | a task for the scheduler with ``--when``/``--est``; the block is a calendar op of planning |
| finding ``message.draft`` | ``draft`` on the ``message_mcp`` destination, to the sender             |
| finding ``now``/``done``/``watch``/``dismiss`` | an outcome row (dispositions are written at close) |
| finding answered by merge | nothing of its own: the host's action carries its title, links, evidence |
| reconcile              | ``task.close`` / ``task.carry`` / ``task.update`` (+ ``agenda.add``)     |
| verify                 | ``task.close`` / ``task.update``; ``keep`` writes nothing                |

Outcome rows (the plan's ``outcomes``)::

    {"question", "item", "topic", "decision", "note", "until"}

Store adjustments tell the scheduler what reconcile and verify answers do to
open tasks before apply has run: ``closed`` tasks are not scheduled (their
future blocks are released) and ``est`` replaces a task's estimate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from docket.config.digest import digest
from docket.decide.answers import MERGED
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

NON_DESTINATION = ("now", "done", "watch", "dismiss")
MERGE_TITLE = "{titles|join:; }"


# --- adapter previews ------------------------------------------------------------------------


def preview(rc: "RunContext", kind: str, dest_id: str | None, fields: dict[str, Any], qid: str, *,
            target: str | None = None, key: str | None = None, item_id: str | None = None,
            option_id: str | None = None) -> list[dict[str, Any]]:
    """Operations from the destination's own ``preview`` (``docket.destinations.planning``)."""
    from docket.destinations.planning import preview_action
    return preview_action(rc, kind, dest_id, fields, qid, target=target, key=key, item_id=item_id,
                          option_id=option_id)


def task_update_fields(rc: "RunContext", ov: dict[str, Any]) -> dict[str, str]:
    """``set`` fields of a ``task.update`` from overrides, named as the task store names them."""
    from docket.text.durations import format_duration
    names = rc.ws.config.planning.tasks.fields
    out = {}
    if ov.get("est"):
        out[names.get("est", "est")] = format_duration(ov["est"])
    if ov.get("due"):
        out[names.get("due", "due")] = str(ov["due"])
    return out


def forum_agenda_destination(rc: "RunContext", forum_id: str | None) -> str | None:
    for forum in rc.ws.config.profile.forums:
        if forum.id == forum_id and forum.agenda and forum.agenda.get("destination"):
            return str(forum.agenda["destination"])
    return None


def agenda_fields(rc: "RunContext", title: str, ov: dict[str, Any]) -> dict[str, Any]:
    """The context of an ``agenda.add`` for a store task: ``--forum``, else the only forum with an agenda."""
    from docket.decide.options import forum_row
    fid = ov.get("forum")
    if fid is None:
        with_agenda = [f.id for f in rc.ws.config.profile.forums if f.agenda]
        fid = with_agenda[0] if len(with_agenda) == 1 else None
    return {"title": ov.get("action") or ov.get("title") or title, "forum": (forum_row(rc, fid) or {}) if fid else {},
            "note": ov.get("note", "")}


# --- findings --------------------------------------------------------------------------------


def absorbed(answers: dict[str, Any], qid: str) -> list[str]:
    return [k for k, v in sorted(answers.items()) if isinstance(v, dict) and v.get("merged_into") == qid]


def finding_context(rc: "RunContext", qid: str, item: dict[str, Any], overrides: dict[str, Any],
                    merged_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The rendering context of a finding's action: the item, derived defaults and every override."""
    from docket.decide.options import derived_defaults, forum_row, item_view
    view = item_view(rc, item)
    dflt = derived_defaults(rc, item)
    merged_items = merged_items or []
    base_title = overrides.get("action") or overrides.get("title") or item.get("title") or ""
    title = base_title
    links = list(overrides.get("links") or [])
    if merged_items:
        titles = [base_title] + [m.get("title") or "" for m in merged_items]
        tpl = rc.ws.config.decisions.templates.get("merge_title", MERGE_TITLE)
        if not (overrides.get("action") or overrides.get("title")):
            title = templates.render(tpl, {"title": base_title, "titles": titles, "count": len(titles)})
        for u in [item.get("url")] + [m.get("url") for m in merged_items]:
            if u and u not in links:
                links.append(u)
    fid = overrides.get("forum") or dflt.get("forum")
    due = overrides.get("due", "")
    if not due and dflt["due_source"] == "deadline":
        due = dflt["due"]
    view.update({
        "question_id": qid, "title": title, "action": overrides.get("action") or title, "due": due,
        "est": overrides.get("est", ""), "when": overrides.get("when", ""), "note": overrides.get("note", ""),
        "area": overrides.get("area") or dflt.get("area") or "", "links": links,
        "forum": forum_row(rc, fid) or {} if fid else {},
        "run": {"id": rc.run.id, "date": rc.run.header.clock.date},
    })
    if merged_items:
        view["evidence"] = [e for it in [item, *merged_items] for e in (it.get("evidence") or [])]
        view["merged"] = [m.get("id") for m in merged_items]
    return view


def finding_ops(rc: "RunContext", qid: str, item: dict[str, Any], option: dict[str, Any],
                overrides: dict[str, Any], merged_items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    from docket.decide.options import default_destination
    kind = option.get("action")
    if kind not in ("task.add", "agenda.add", "message.draft"):
        return []
    ctx = finding_context(rc, qid, item, overrides, merged_items)
    dest = option.get("destination") or default_destination(rc, kind, None)
    if kind == "agenda.add":
        dest = forum_agenda_destination(rc, (ctx.get("forum") or {}).get("id")) or dest
    key = None
    if kind == "message.draft":
        handle = (item.get("sender") or {}).get("handle") or item.get("from")
        ctx["to"] = [handle] if handle else []
        key = qid
    return preview(rc, kind, dest, ctx, qid, key=key, item_id=item.get("id"), option_id=option.get("id"))


def outcome(qid: str, item: dict[str, Any], decision: str, overrides: dict[str, Any]) -> dict[str, Any]:
    until = overrides.get("due") if decision in ("dismiss", "watch") else None
    return {"question": qid, "item": item.get("id"), "topic": item.get("topic") or item.get("id"),
            "decision": decision, "note": overrides.get("note"), "until": until}


# --- the plan-stage mapping ------------------------------------------------------------------


def _option(q: dict[str, Any], oid: str) -> dict[str, Any]:
    return next((o for o in q["options"] if o["id"] == oid), {"id": oid})


def answer_effects(rc: "RunContext", decide: dict[str, Any], qid: str) -> dict[str, Any]:
    """Operations, outcomes, store adjustment and notes of one bound answer."""
    q = next(x for x in decide["questions"] if x["id"] == qid)
    ans = decide["answers"][qid]
    ov = ans.get("overrides") or {}
    out: dict[str, Any] = {"operations": [], "outcomes": [], "adjust": {}, "notes": []}
    if ans.get("option") == MERGED:
        return out
    if q["kind"] == "finding":
        items = {f["question_id"]: f["item"] for f in decide["findings"]}
        item = items[qid]
        merged = [items[k] for k in absorbed(decide["answers"], qid) if k in items]
        action = ans.get("action")
        if action in NON_DESTINATION:
            out["outcomes"] = [outcome(qid, item, action, ov)] + [outcome(k, items[k], action, ov)
                                                                 for k in absorbed(decide["answers"], qid) if k in items]
        elif action == "calendar.place":
            from docket.plan.build import planning_active
            if not planning_active(rc):
                out["notes"].append(f"{qid}: a calendar block needs planning.calendar; nothing was placed.")
        else:
            out["operations"] = finding_ops(rc, qid, item, _option(q, ans["option"]), ov, merged)
        return out
    rows = {r["question_id"]: r for r in decide.get(q["kind"]) or []}
    row = rows.get(qid)
    if row is None:
        return out
    mod = __import__("docket.decide." + q["kind"], fromlist=["operations"])
    ops, adj = mod.operations(rc, qid, row, ans)
    out["operations"], out["adjust"] = ops, ({row["task"]["id"]: adj} if adj else {})
    if q["kind"] == "verify" and ans["option"] == "keep":
        out["notes"].append(f"{qid}: kept open after verification ({row['reason']}).")
    return out


def plan_operations(rc: "RunContext", decide: dict[str, Any]) -> dict[str, Any]:
    """Everything the plan stage takes from decide: operations, outcomes, store adjustments, notes."""
    ops: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    adjust: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    seen_targets: dict[tuple[Any, ...], str] = {}
    for qid in sorted(decide["answers"]):
        if not any(q["id"] == qid for q in decide["questions"]):
            continue
        eff = answer_effects(rc, decide, qid)
        for op in eff["operations"]:
            tgt = (op["destination"], op.get("path"), op.get("anchor"), op.get("old_digest")) \
                if op.get("old_digest") else None
            if tgt is not None and tgt in seen_targets:
                notes.append(f"{qid}: {op['destination']} line already changed by {seen_targets[tgt]}; "
                             f"the second change was dropped.")
                continue
            if tgt is not None:
                seen_targets[tgt] = qid
            ops.append(op)
        outcomes += eff["outcomes"]
        for tid, adj in eff["adjust"].items():
            adjust.setdefault(tid, {}).update(adj)
        notes += eff["notes"]
    return {"operations": ops, "outcomes": outcomes, "adjust": adjust, "notes": notes}


def scheduling_inputs(rc: "RunContext", decide: dict[str, Any]) -> list[dict[str, Any]]:
    """Finding answers the scheduler places (``task.add`` and ``calendar.place``), with their task-store
    line when the answer adds one."""
    out = []
    items = {f["question_id"]: f["item"] for f in decide.get("findings") or []}
    qs = {q["id"]: q for q in decide["questions"]}
    for qid in sorted(decide["answers"]):
        ans = decide["answers"][qid]
        if ans.get("action") not in ("task.add", "calendar.place") or qid not in items:
            continue
        ov = dict(ans.get("overrides") or {})
        item = items[qid]
        merged = [items[k] for k in absorbed(decide["answers"], qid) if k in items]
        ctx = finding_context(rc, qid, item, ov, merged)
        title = ov.get("title") or ov.get("action") or ctx["title"]
        op = None
        if ans.get("action") == "task.add":
            ops = finding_ops(rc, qid, item, _option(qs[qid], ans["option"]), ov, merged)
            op = ops[0] if ops else None
        out.append({"question_id": qid, "action": ans["action"], "title": title, "overrides": ov, "op": op})
    return out


def answer_digest(decide: dict[str, Any]) -> str:
    return digest(decide.get("answers") or {})


__all__ = ["plan_operations", "scheduling_inputs", "answer_effects", "finding_ops", "finding_context", "preview",
           "task_update_fields", "agenda_fields", "outcome", "NON_DESTINATION"]
