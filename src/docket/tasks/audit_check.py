"""The rules an ``audit`` row keeps that need its pack item (TD §9.7).

The output model checks each field. These rules check the row against the forum it judges:

- every current agenda entry (``fields.entries``, N1, N2, ...) is answered exactly once, and
  no row names an entry the agenda does not have (``task.audit_entries``);
- a ``reword`` gives the new wording in ``text`` (``task.cross_field``);
- an addition's ``link`` is a link that appears in the pack (``task.unknown_link``);
- an addition's ``evidence`` appears word for word in the pack, spacing aside
  (``task.evidence_not_verbatim``).

Each error names the row, the path, the value received, the rule and the fix, so the agent
that wrote the row can correct it and submit again.
"""
from __future__ import annotations

import re
from typing import Any

_URL = re.compile(r"https?://[^\s<>\"'\])]+")


def strings(v: Any) -> list[str]:
    """Every string inside ``v``, depth first."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in strings(x)]
    if isinstance(v, (list, tuple)):
        return [s for x in v for s in strings(x)]
    return []


def links_in(v: Any) -> list[str]:
    """Every web link in the strings of ``v``, in order, each once; trailing punctuation is not part of one."""
    out: list[str] = []
    for s in strings(v):
        for m in _URL.finditer(s):
            u = m.group(0).rstrip(".,;:!?")
            if u not in out:
                out.append(u)
    return out


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


class AuditCheck:
    def __init__(self, max_add: int) -> None:
        self.max_add = max_add

    def row_errors(self, item: dict[str, Any], row: dict[str, Any], base: str) -> list[dict[str, Any]]:
        rid = row["id"]
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        lines = [str(e.get("line")) for e in fields.get("entries") or [] if isinstance(e, dict)]
        out: list[dict[str, Any]] = []

        def err(path: str, received: Any, code: str, rule: str, fix: str) -> None:
            out.append({"id": rid, "path": f"{base}.{path}", "received": received, "code": code, "rule": rule,
                        "fix": fix})

        known = ", ".join(lines) if lines else "none: the agenda has no entries, so entries is empty"
        seen: dict[str, int] = {}
        for j, e in enumerate(row.get("entries") or []):
            line = str(e.get("line") or "")
            if line not in lines:
                err(f"entries[{j}].line", line, "task.audit_entries", "an entry row names a current agenda entry",
                    f"use one of: {known}")
            elif line in seen:
                err(f"entries[{j}].line", line, "task.audit_entries", "every current entry is answered exactly once",
                    f"remove this row or the one at entries[{seen[line]}]")
            else:
                seen[line] = j
            if e.get("verdict") == "reword" and not str(e.get("text") or "").strip():
                err(f"entries[{j}].text", e.get("text"), "task.cross_field",
                    "a reword gives the new one-line wording in text", "add `text`, or make the verdict keep")
        missing = [ln for ln in lines if ln not in seen]
        if missing:
            err("entries", sorted(seen, key=lines.index), "task.audit_entries",
                "every current entry is answered exactly once",
                f"add a row for {', '.join(missing)} with its verdict")
        allowed = links_in(item)
        hay = _norm("\n".join(strings(item)))
        for j, a in enumerate(row.get("add") or []):
            link = a.get("link")
            if link and link not in allowed:
                err(f"add[{j}].link", link, "task.unknown_link", "a link must appear in the pack",
                    "copy a link exactly from the pack, or leave link out")
            ev = a.get("evidence")
            if ev and _norm(ev) not in hay:
                err(f"add[{j}].evidence", ev, "task.evidence_not_verbatim",
                    "evidence must appear word for word in the pack (only spacing may differ)",
                    "copy the words exactly from the pack, or leave evidence out")
        return out

    def example(self, item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        """The model's example row made true of ``item``: every entry kept, nothing added."""
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        entries = [{"line": str(e.get("line")), "verdict": "keep", "why": "It still needs the two of them"}
                   for e in fields.get("entries") or [] if isinstance(e, dict)]
        return {**row, "entries": entries, "add": []}

    def example_item(self, row: dict[str, Any]) -> dict[str, Any]:
        """A pack item a workspace example row is true of: its entries, and its additions' links
        and evidence as the pack's words."""
        entries = [{"line": str(e.get("line")), "text": f"- entry {e.get('line')}"}
                   for e in row.get("entries") or [] if isinstance(e, dict) and e.get("line")]
        said = [str(a.get(k)) for a in row.get("add") or [] if isinstance(a, dict)
                for k in ("link", "evidence") if a.get(k)]
        return {"fields": {"entries": entries, "said": said}}


__all__ = ["AuditCheck", "links_in", "strings"]
