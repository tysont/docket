"""Open tasks of the task store, read through its ``markdown_tasks`` adapter (TD §1 principle 5, §11.3).

There is one line grammar: the destination's (``docket.text.grammar``), read by
the adapter that also writes, closes and carries the lines. This module only
maps a task's line fields to planning attributes through
``planning.tasks.fields`` (``due``, ``est``, ``start``, ``tier``, ``when``,
``carry``, ``id``, ``deadline``; ``carry`` defaults to the destination's
``carry_field``) and parses their values. ``planning.tasks.sections`` limits the
headings read; a task's tier is its section's position there unless it carries
a tier field.

A task's id is the adapter's (``MarkdownTasks.task_id``): the line's id field,
else the destination and its normalized title. The line an answer appends this
run and the same line read next run share an id and therefore an ownership key.
Unparseable field values are dropped with an unexpected-shape record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from docket.config.digest import sha256_hex
from docket.shapes.unexpected import unexpected

if TYPE_CHECKING:
    from docket.engine.context import RunContext

FIELD_NAMES = ("due", "est", "start", "tier", "when", "carry", "id", "deadline")


@dataclass(frozen=True)
class StoreTask:
    id: str
    title: str
    section: str
    line_no: int
    fields: dict[str, str]  # planning attribute -> value
    tier: int
    line: str = ""
    destination: str = ""
    raw: dict[str, str] = field(default_factory=dict)  # field name on the line -> value


def _field_names(rc: "RunContext", did: str) -> dict[str, str]:
    fmap = rc.ws.config.planning.tasks.fields or {}
    carry = rc.ws.config.destinations[did].carry_field or "carry"
    return {a: fmap.get(a) or (carry if a == "carry" else a) for a in FIELD_NAMES}


def _attributes(raw: dict[str, str], names: dict[str, str]) -> dict[str, str]:
    low = {k.lower(): v.strip() for k, v in raw.items()}
    return {a: low[n.lower()] for a, n in names.items() if low.get(n.lower())}


def _adapter(rc: "RunContext", did: str, *, live: bool = False) -> Any:
    from docket.destinations import build, env_for, for_run
    cfg = rc.ws.config.destinations.get(did)
    if cfg is None or cfg.adapter != "markdown_tasks":
        unexpected("plan.tasks", "task store is not a markdown_tasks destination", sample=str(did)[:40],
                   reading="read no tasks", extend="planning.tasks.destination")
        return None
    return build(did, cfg, env_for(rc.ws, run=rc.run, env=rc.env)) if live else for_run(rc, did)


def _store_task(o: Any, did: str, names: dict[str, str]) -> StoreTask:
    return StoreTask(o.id, o.title, o.section, o.index + 1, _attributes(o.fields, names), o.tier, o.line, did,
                     dict(o.fields))


def read_store(rc: "RunContext", did: str, *, sections: list[str] | None = None) -> tuple[list[StoreTask], str]:
    """Open tasks and the store digest (``absent`` when the file does not exist)."""
    dest = _adapter(rc, did)
    if dest is None:
        return [], "absent"
    names = _field_names(rc, did)
    try:
        state = dest.snapshot()
        text = state.data.get("text")
        if text is None:
            return [], "absent"
        rows = dest.open_tasks(state, sections=sections, id_field=names["id"])
    finally:
        dest.close()
    return [_store_task(o, did, names) for o in rows], sha256_hex(text.encode("utf-8"))


def parse_line(rc: "RunContext", did: str, line: str) -> StoreTask | None:
    """The task a line will be once written to destination ``did``, or None when it is not an open task there."""
    cfg = rc.ws.config.destinations.get(did)
    if cfg is None or cfg.adapter != "markdown_tasks":  # another adapter's line: the answer keeps its own id
        return None
    dest = _adapter(rc, did, live=True)
    names = _field_names(rc, did)
    try:
        o = dest.read_line(line, id_field=names["id"])
    finally:
        dest.close()
    return None if o is None else _store_task(o, did, names)


def parse_field_date(v: str, ref: date) -> date | None:
    from docket.cli.errors import InputError
    from docket.text.dates import parse_date
    try:
        return parse_date(v, ref)
    except InputError:
        unexpected("plan.tasks", "unparseable date field", sample=v[:40], reading="ignore the field",
                   extend="planning.tasks.fields, or fix the task line")
        return None


def parse_field_minutes(v: str) -> int | None:
    from docket.cli.errors import InputError
    from docket.text.durations import parse_duration
    try:
        return parse_duration(v)
    except InputError:
        unexpected("plan.tasks", "unparseable duration field", sample=v[:40], reading="use the default estimate",
                   extend="planning.tasks.fields, or fix the task line")
        return None


def parse_field_int(v: str, what: str) -> int | None:
    try:
        return int(v)
    except ValueError:
        unexpected("plan.tasks", f"unparseable {what} field", sample=v[:40], reading="ignore the field",
                   extend="planning.tasks.fields, or fix the task line")
        return None


__all__: list[Any] = ["read_store", "parse_line", "StoreTask", "FIELD_NAMES"]
