"""Record-to-item mapping, M1 form (TD §7.1, §7.2).

A mapping names a ``$.path`` per item field. A path that is absent in a record
renders empty and records an unexpected shape naming the mapping to extend.
"""
from __future__ import annotations

from typing import Any

from docket.config.digest import digest
from docket.shapes.unexpected import unexpected

DEFAULT_MAPPING = {"id": "$.id", "title": "$.title", "text": "$.text"}
ITEM_FIELDS = ("title", "text", "from", "at", "url", "unit")


def get_path(record: Any, path: str) -> Any:
    cur = record
    for part in path[2:].split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def item_id(source: str, unit: str, identity: str) -> str:
    return digest([source, unit, identity])[:16]


def map_record(source: str, record: dict[str, Any], mapping: dict[str, str], *, fallback_identity: str) -> dict[str, Any]:
    m = {**DEFAULT_MAPPING, **mapping}
    ident = get_path(record, m["id"])
    if ident is None or ident == "":
        unexpected("item.mapping", f"{source}.id missing", sample=record,
                   reading="identify the record by its content digest",
                   extend=f"sources.{source}.mapping.id")
        ident = fallback_identity
    unit = get_path(record, m["unit"]) if "unit" in m else None
    item: dict[str, Any] = {
        "id": item_id(source, str(unit or ""), str(ident)),
        "source": source,
        "unit": "" if unit is None else str(unit),
        "identity": str(ident),
    }
    for f in ITEM_FIELDS:
        if f == "unit":
            continue
        path = m.get(f)
        val = get_path(record, path) if path else None
        if path and val is None:
            unexpected("item.mapping", f"{source}.{f} missing",
                       reading="render the field empty", extend=f"sources.{source}.mapping.{f}")
        item[f] = "" if val is None else (val if isinstance(val, (str, int, float, bool)) else str(val))
    if not item["title"]:
        text = str(item.get("text") or "")
        item["title"] = text[:80]
    item["fields"] = {
        k.split(".", 1)[1]: get_path(record, v) for k, v in sorted(m.items()) if k.startswith("fields.")
    }
    return item
