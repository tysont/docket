"""Record-to-item mapping (TD §7.1, §7.2).

A mapping names a ``$.path`` per item field. A path that is absent in a record
renders empty and records an unexpected shape naming the mapping to extend.

Structure keys (M3) are optional per record and never a shape when absent: an
unthreaded message or one with no recipients is a valid record.

- ``from.handle`` / ``from.display``: the sender's handle (resolved later through
  profile handles, ``docket.items.identity``) and display name. ``from`` stays the
  display string templates render; without ``from.handle`` the handle is ``from``.
- ``thread``: the source's thread key (the item id stands in for unthreaded records).
- ``to``, ``cc``, ``mentions``: handles (a string or a list) addressed, copied or mentioned.
"""
from __future__ import annotations

from typing import Any

from docket.config.digest import digest
from docket.shapes.unexpected import unexpected

DEFAULT_MAPPING = {"id": "$.id", "title": "$.title", "text": "$.text"}
ITEM_FIELDS = ("title", "text", "from", "at", "url", "unit")
STRUCTURE_FIELDS = ("from.handle", "from.display", "thread", "to", "cc", "mentions")


def _scalar(v: Any) -> str:
    return "" if v is None else (v if isinstance(v, str) else str(v))


def _handles(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):  # a recipient object: take its address-like field
                x = next((x[k] for k in ("handle", "email", "address", "name", "id") if x.get(k)), None)
            if x not in (None, ""):
                out.append(str(x))
        return out
    return [str(v)]


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
    st = {k: (get_path(record, m[k]) if m.get(k) else None) for k in STRUCTURE_FIELDS}
    handle = _scalar(st["from.handle"]) if "from.handle" in m else _scalar(item.get("from"))
    if st["from.display"] not in (None, ""):
        item["from"] = _scalar(st["from.display"])
    elif not item.get("from") and handle:
        item["from"] = handle
    item["handle"] = handle
    item["thread"] = _scalar(st["thread"])
    for k in ("to", "cc", "mentions"):
        item[k] = _handles(st[k])
    item["fields"] = {
        k.split(".", 1)[1]: get_path(record, v) for k, v in sorted(m.items()) if k.startswith("fields.")
    }
    return item
