"""From an answer to operations: the bridge between decide/plan and the destinations.

``answer_operations`` renders the action an answer chose through its
destination's ``preview`` against a fresh ``snapshot``, so the plan holds the
exact operations apply will perform (quality rules, redaction and ownership
included). Calendar placement and message drafts arrive with planning (M7) and
the executor work of M8; an answer whose action writes nothing returns no
operation.
"""
from __future__ import annotations

from typing import Any

from docket.destinations import for_run
from docket.destinations.base import Action

ACTION_ADAPTER = {"task.add": "markdown_tasks", "agenda.add": "markdown_agenda"}


def default_destination(ws: Any, action: str, named: str | None) -> str | None:
    if named:
        return named
    want = ACTION_ADAPTER.get(action)
    for name in sorted(ws.config.destinations):
        if ws.config.destinations[name].adapter == want:
            return name
    return None


def answer_context(rc: Any, qid: str, item: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    from docket.engine.stages import item_context
    ctx = item_context(item)
    ctx.update({
        "question_id": qid, "title": overrides.get("title") or ctx["title"], "due": overrides.get("due", ""),
        "est": overrides.get("est", ""), "when": overrides.get("when", ""), "note": overrides.get("note", ""),
        "area": "", "run": {"id": rc.run.id, "date": rc.run.header.clock.date},
    })
    return ctx


def answer_operations(rc: Any, qid: str, item: dict[str, Any], option: dict[str, Any],
                      overrides: dict[str, Any]) -> list[dict[str, Any]]:
    kind = option.get("action")
    if kind not in ACTION_ADAPTER:
        return []
    dest_id = default_destination(rc.ws, kind, option.get("destination"))
    if dest_id is None:
        return []
    action = Action(kind=kind, destination=dest_id, fields=answer_context(rc, qid, item, overrides),
                    question_id=qid, item_id=item["id"], option_id=option.get("id"))
    dest = for_run(rc, dest_id)
    try:
        def fix(flag: str, placeholder: str) -> str:
            return " ".join(rc.argv("ask", "answer", qid, str(option.get("id")), flag, placeholder))
        ops = dest.preview(action, dest.snapshot(), fix=fix)
    finally:
        dest.close()
    return [op.dump() for op in ops]
