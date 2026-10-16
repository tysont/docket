"""Request plans: request templates rendered against a read context.

A string that is exactly ``{name}`` is replaced by the typed value (an integer
page size stays an integer); a key whose value renders to ``None`` is dropped,
so an absent page token sends no token. Other strings substitute ``{name}``
with the value's text. Names are dotted paths into the context: ``unit.id``,
``unit.name``, ``cursor.position``, ``interval.from``, ``interval.to``,
``page.size``, ``page.token``, ``page.number``, ``params.<key>``, ``id``,
``ref``, ``match.<group>``, ``limit``. A ``script`` key is left alone: the
codemode transport renders its own ``{{name}}`` placeholders.
"""
from __future__ import annotations

import re
from typing import Any

_WHOLE = re.compile(r"^\{([a-z_][a-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)\}$")
_PART = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)\}")
_DROP = object()


def _lookup(ctx: dict[str, Any], name: str) -> Any:
    cur: Any = ctx
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _render(value: Any, ctx: dict[str, Any], key: str | None = None) -> Any:
    if key == "script":
        return value
    if isinstance(value, str):
        m = _WHOLE.match(value)
        if m:
            v = _lookup(ctx, m.group(1))
            return _DROP if v is None else v
        return _PART.sub(lambda mm: "" if _lookup(ctx, mm.group(1)) is None else str(_lookup(ctx, mm.group(1))), value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            r = _render(v, ctx, k)
            if r is not _DROP:
                out[k] = r
        return out
    if isinstance(value, list):
        return [r for r in (_render(v, ctx) for v in value) if r is not _DROP]
    return value


def render(template: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    out = _render(template, ctx)
    return out if isinstance(out, dict) else {}
