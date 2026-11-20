"""The open task lines a review task sees (TD §9.2).

A candidate can be the same matter as a task the operator already holds, under another link.
The merge node's ``tracked`` check only sees a link or a reference written on the line, so the
review reads the open lines of every ``markdown_tasks`` destination (``task_stores``) and
judges sameness itself.

Each line gets a handle, ``T1``, ``T2``, ..., in destination id order and then file order, so
the handles are the same in every pack of one evaluation. The pack shows at most
``tasks.review.max_open_tasks`` lines, each cut to ``OPEN_TASK_CHARS`` characters, and
``omitted`` counts the rest. A line's id field, when it has one, is kept as ``task_id``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from docket.config.models import DocketConfig

OPEN_TASK_CHARS = 300


def _id_field(config: "DocketConfig") -> str:
    # The planning attribute ``id`` names the field; a configured name is used as it is.
    fmap = config.planning.tasks.fields or {}
    return fmap["id"] if fmap.get("id") is not None else "id"


def entries(stores: list[dict[str, Any]], config: "DocketConfig") -> list[dict[str, Any]]:
    """Every open line of ``stores`` (``task_stores`` rows, in destination order), each with
    its handle, destination, whole line and, when the line has one, its id."""
    from docket.text.grammar import Grammar
    id_field = _id_field(config)
    out: list[dict[str, Any]] = []
    for st in stores:
        did = str(st.get("destination") or "")
        cfg = config.destinations.get(did)
        grammar = Grammar.from_config(cfg.grammar) if cfg is not None else None
        for line in st.get("open") or []:
            t = grammar.parse(line) if grammar is not None else None
            tid = t.get(id_field) if t is not None else None
            out.append({"handle": f"T{len(out) + 1}", "destination": did, "line": line,
                        **({"task_id": tid.strip()} if tid and tid.strip() else {})})
    return out


def read(ws: Any, run: Any) -> list[dict[str, Any]]:
    """The open task lines of the workspace's task stores, with handles."""
    from docket.signals.node import task_stores
    return entries(task_stores(ws, run), ws.config)


def block(tasks: list[dict[str, Any]], cap: int) -> dict[str, Any] | None:
    """The pack's ``open_tasks`` block: the first ``cap`` lines, cut, and how many were left
    out. None when the cap is 0 or there is no open task."""
    if cap <= 0 or not tasks:
        return None
    lines = []
    for t in tasks[:cap]:
        text = " ".join(str(t["line"]).split())
        if len(text) > OPEN_TASK_CHARS:
            text = text[:OPEN_TASK_CHARS] + "\u2026"
        lines.append({"handle": t["handle"], "destination": t["destination"], "text": text})
    return {"lines": lines, "omitted": max(0, len(tasks) - cap)}


__all__ = ["OPEN_TASK_CHARS", "block", "entries", "read"]
