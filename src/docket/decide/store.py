"""Open tasks of the task store, read through its destination adapter (TD §10.4, §10.5).

Reconcile and verification read the task store the way apply will write it:
through the ``markdown_tasks`` adapter's grammar, so the carry count is the
adapter's ``carry_field`` and the line text is exactly what a ``task.close``,
``task.carry`` or ``task.update`` operation will target. A task's id is the
adapter's (``MarkdownTasks.task_id``, read through ``docket.plan.taskstore``), the
same id planning uses, so a block's ownership key maps back to its task.
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
    from docket.plan.taskstore import read_store
    did = store_destination(rc)
    if did is None:
        return []
    rows, _ = read_store(rc, did)
    carry_name = rc.ws.config.destinations[did].carry_field or "carry"
    out = []
    for r in rows:
        raw = r.raw.get(carry_name)
        try:
            carry = int(raw or 0)
        except ValueError:
            unexpected("decide.store", "carry field is not a number", sample=str(raw)[:20],
                       reading="count no carries", extend=f"destinations.{did}.carry_field")
            carry = 0
        out.append(StoreTask(r.id, r.title, r.line, r.section, did, carry, dict(r.raw)))
    return out


__all__ = ["StoreTask", "open_tasks", "block_key", "store_destination"]
