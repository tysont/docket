"""Open tasks of the task store, read through its destination adapter (TD §10.4, §10.5).

Reconcile and verification read the task store the way apply will write it:
through the ``markdown_tasks`` adapter's grammar, so the carry count is the
adapter's ``carry_field`` and the line text is exactly what a ``task.close``,
``task.carry`` or ``task.update`` operation will target. A task's id is the
planning id (``docket.plan.taskstore``) so a block's ownership key maps back to
its task.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from docket.config.digest import digest
from docket.shapes.unexpected import unexpected

if TYPE_CHECKING:
    from docket.engine.context import RunContext


@dataclass
class StoreTask:
    id: str
    title: str
    line: str
    section: str
    destination: str
    carry: int = 0
    fields: dict[str, str] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "line": self.line, "section": self.section,
                "destination": self.destination, "carry": self.carry,
                "est": self.fields.get("est", ""), "fields": dict(self.fields)}


def store_destination(rc: "RunContext") -> str | None:
    from docket.plan.build import store_destination as sd
    did = sd(rc)
    if did is None or did not in rc.ws.config.destinations:
        return None
    return did if rc.ws.config.destinations[did].adapter == "markdown_tasks" else None


def block_key(prefix: str, task_id: str) -> str:
    """The ownership key of a task's block (``docket.plan.types.task_key`` for a single task)."""
    return f"{prefix}t_{digest(['task', task_id])[:12]}"


def open_tasks(rc: "RunContext") -> list[StoreTask]:
    from docket.destinations import for_run
    from docket.plan.taskstore import parse_line, task_id
    did = store_destination(rc)
    if did is None:
        return []
    dest = for_run(rc, did)
    try:
        state = dest.snapshot()
        rows = dest.tasks(state)
        carry_name = dest.cfg.carry_field or "carry"
    finally:
        dest.close()
    fmap = rc.ws.config.planning.tasks.fields
    out = []
    for r in rows:
        t = r["task"]
        if t.status != "open":
            continue
        parsed = parse_line(r["line"], did, fmap)
        if parsed is not None:
            tid, title, _ = parsed
        else:
            title = " ".join(t.text.split())
            tid = task_id(did, title)
        raw = t.get(carry_name)
        try:
            carry = int(raw or 0)
        except ValueError:
            unexpected("decide.store", "carry field is not a number", sample=str(raw)[:20],
                       reading="count no carries", extend=f"destinations.{did}.carry_field")
            carry = 0
        fields = {k: v for k, v in t.fields}
        out.append(StoreTask(tid, title, r["line"], r["section"] or "", did, carry, fields))
    return out


__all__ = ["StoreTask", "open_tasks", "block_key", "store_destination"]
