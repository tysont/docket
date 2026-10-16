"""From an answer to operations: the bridge between decide/plan and the destinations.

``preview_action`` renders one action through its destination's ``preview``
against a fresh ``snapshot``, so the plan holds the exact operations apply
will perform (quality rules, redaction and ownership included). Every answer
kind reaches the destinations through it; the mapping from answers to actions
is ``docket.decide.operations`` (M5). ``answer_operations`` is the finding
entry point kept for callers of the M6 interface.
"""
from __future__ import annotations

from typing import Any

from docket.destinations import for_run
from docket.destinations.base import Action

ACTION_ADAPTER = {"task.add": "markdown_tasks", "agenda.add": "markdown_agenda", "message.draft": "message_mcp"}


def default_destination(ws: Any, action: str, named: str | None) -> str | None:
    if named:
        return named
    want = ACTION_ADAPTER.get(action)
    for name in sorted(ws.config.destinations):
        if ws.config.destinations[name].adapter == want:
            return name
    return None


def preview_action(rc: Any, kind: str, dest_id: str | None, fields: dict[str, Any], qid: str, *,
                   target: str | None = None, key: str | None = None, item_id: str | None = None,
                   option_id: str | None = None) -> list[dict[str, Any]]:
    """Operations for one action, previewed by the destination the run writes (its twin in a sandbox run).

    A quality refusal names the ``docket ask answer`` override that fixes it.
    """
    if dest_id is None or dest_id not in rc.ws.config.destinations:
        return []
    action = Action(kind=kind, destination=dest_id, fields=fields, target=target, key=key, question_id=qid,
                    item_id=item_id, option_id=option_id)
    dest = for_run(rc, dest_id)
    try:
        def fix(flag: str, placeholder: str) -> str:
            return " ".join(rc.argv("ask", "answer", qid, str(option_id or "<option>"), flag, placeholder))
        if dest.adapter == "markdown_tasks":
            ops = dest.preview(action, dest.snapshot(), fix=fix)
        else:
            ops = dest.preview(action, dest.snapshot())
    finally:
        dest.close()
    return [op.dump() for op in ops]


def answer_operations(rc: Any, qid: str, item: dict[str, Any], option: dict[str, Any],
                      overrides: dict[str, Any]) -> list[dict[str, Any]]:
    from docket.decide.operations import finding_ops
    return finding_ops(rc, qid, item, option, overrides)
