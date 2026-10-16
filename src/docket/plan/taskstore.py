"""Open tasks from a Markdown task store, for planning.

An interim reader until the task-store adapter exposes a parsed snapshot
(M6): open checkbox lines (``- [ ] ...``) with bracketed inline fields
(``[due:: 2026-10-20]``, ``[est:: 45m]``, ``[start:: ...]``, ``[tier:: 2]``,
``[when:: thu 13:00-15:00]``, ``[carry:: 1]``, ``[id:: ...]``). Field names map
through ``planning.tasks.fields``. ``planning.tasks.sections`` limits the
headings read; a task's tier is its section's position there unless it
carries a tier field.

A task's id derives from the destination and its normalized title, so the
line an answer appends this run and the same line read next run share an id
and therefore an ownership key. Unparseable field values are dropped with an
unexpected-shape record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from docket.config.digest import digest, sha256_hex
from docket.shapes.unexpected import unexpected

_OPEN = re.compile(r"^\s*[-*]\s+\[ \]\s+(.*)$")
_FIELD = re.compile(r"\[([A-Za-z_][\w-]*)::\s*([^\]]*)\]")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|\[([^\]]+)\]\([^)]+\)")
FIELD_NAMES = ("due", "est", "start", "tier", "when", "carry", "id", "deadline")


@dataclass(frozen=True)
class StoreTask:
    id: str
    title: str
    section: str
    line_no: int
    fields: dict[str, str]
    tier: int


def clean_title(text: str) -> str:
    t = _FIELD.sub("", text)
    t = _LINK.sub(lambda m: m.group(2) or m.group(1) or m.group(3) or "", t)
    return " ".join(t.split())


def task_id(destination: str, title: str) -> str:
    return "t_" + digest([destination, title.casefold()])[:16]


def parse_line(line: str, destination: str, field_map: dict[str, str] | None = None) -> tuple[str, str, dict[str, str]] | None:
    """(id, title, fields) of an open task line, or None."""
    m = _OPEN.match(line)
    if not m:
        return None
    body = m.group(1)
    raw = {k.lower(): v.strip() for k, v in _FIELD.findall(body)}
    fmap = {a: (field_map or {}).get(a, a) for a in FIELD_NAMES}
    fields = {a: raw[name.lower()] for a, name in fmap.items() if name.lower() in raw and raw[name.lower()]}
    title = clean_title(body)
    tid = fields.get("id") or task_id(destination, title)
    return tid, title, fields


def read_store(path: Path, destination: str, *, sections: list[str] | None = None,
               field_map: dict[str, str] | None = None) -> tuple[list[StoreTask], str]:
    """Open tasks and the store digest (``absent`` when the file does not exist)."""
    if not path.is_file():
        return [], "absent"
    data = path.read_bytes()
    text = data.decode("utf-8", "replace")
    wanted = [s.strip() for s in sections] if sections else None
    current = ""
    out: list[StoreTask] = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            current = line.strip()
            continue
        if wanted is not None and current not in wanted:
            continue
        parsed = parse_line(line, destination, field_map)
        if parsed is None:
            continue
        tid, title, fields = parsed
        tier = wanted.index(current) if wanted is not None else 0
        out.append(StoreTask(tid, title, current, n, fields, tier))
    return out, sha256_hex(data)


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


__all__: list[Any] = ["read_store", "parse_line", "task_id", "StoreTask"]
