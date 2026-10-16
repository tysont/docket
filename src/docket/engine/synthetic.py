"""Synthetic task results for rehearsal (TD §14.3, §20.1).

A rehearsal must exercise decide, plan and apply on realistic shapes, so a
delegated stage without an accepted result gets a synthetic one. It is built
from the kind's own field declarations (the same ones the prompt and the
validator read), so it is schema-valid by construction, and it is
conservative:

- an item is kept (``keep: true``; always for a must-surface item), so the
  rehearsal never drops what a live agent might have asked about;
- an enum takes its declared example, else its safe reading, else its first value;
- evidence is the item's first pack line, quoted verbatim with its line id;
- optional fields stay empty unless a cross-field rule requires them.

The rehearsal labels each such result as synthetic in its report and keeps
the result file under the rehearse directory.
"""
from __future__ import annotations

from typing import Any


def _quote(item: dict[str, Any], spec: Any) -> list[dict[str, str]]:
    for ln in item.get("lines") or []:
        text = str(ln.get("text") or "").strip()
        if text:
            return [{"line": ln["line"], "quote": text[: spec.max_quote].strip()}]
    return []


def _title(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("title") or item.get("id") or "the item").split())[:80]


def _value(spec: Any, item: dict[str, Any], run_date: str, *, kind: str) -> Any:
    t = spec.type
    if spec.echo:
        return item.get(spec.echo)
    if t == "bool":
        if spec.name == "keep":
            return True
        return spec.example if isinstance(spec.example, bool) else False
    if t == "enum":
        names = spec.value_names()
        if spec.example in names:
            return spec.example
        return spec.safe if spec.safe in names else (names[0] if names else spec.safe)
    if t == "int":
        if isinstance(spec.example, int) and spec.lo <= spec.example <= spec.hi:
            return spec.example
        return spec.lo
    if t == "evidence":
        return _quote(item, spec)
    if t == "date":
        return run_date
    if t == "duration":
        return 30
    if t == "item_ref":
        return item.get("id")
    if t == "rows":
        return []
    if spec.name == "owner":
        return "me"
    if spec.name == "ask" or spec.default_verb:
        text = f"{spec.default_verb or 'Follow up on'} {_title(item)}"
    else:
        text = f"Synthetic rehearsal value for {_title(item)}"
    return text[: spec.max_length] if spec.max_length else text


def synthetic_row(kind: Any, item: dict[str, Any], run_date: str) -> dict[str, Any]:
    row: dict[str, Any] = {"id": item["id"]}
    for f in kind.fields:
        if f.required or f.echo or f.name == "keep":
            row[f.name] = _value(f, item, run_date, kind=kind.name)
    for _ in range(3):  # rules can chain: a field one rule requires can satisfy another's condition
        changed = False
        for r in kind.rules:
            for name in r.missing(row):
                f = kind.field(name)
                if f is not None:
                    row[name] = _value(f, item, run_date, kind=kind.name)
                    changed = True
        if not changed:
            break
    for f in kind.fields:  # an evidence field no rule required: quote anyway when the item has a line
        if f.type == "evidence" and f.name not in row:
            q = _quote(item, f)
            if q:
                row[f.name] = q
    return row


def synthetic_result(kind: Any, pack: dict[str, Any], run_date: str) -> dict[str, Any]:
    return {"items": [synthetic_row(kind, it, run_date) for it in pack.get("items") or []]}
