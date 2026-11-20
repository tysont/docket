"""The rules a ``review`` row keeps beyond its declared cross-field rules (TD §9.4).

- ``deadline`` and ``no_deadline: true`` exclude each other: one names a due date, the
  other says the item has none (``task.cross_field``).

Each error names the row, the path, the value received, the rule and the fix, so the agent
that wrote the row can correct it and submit again.
"""
from __future__ import annotations

from typing import Any


class ReviewCheck:
    def row_errors(self, item: dict[str, Any], row: dict[str, Any], base: str) -> list[dict[str, Any]]:
        rid = row["id"]
        out: list[dict[str, Any]] = []
        if row.get("no_deadline") is True and row.get("deadline") not in (None, ""):
            out.append({"id": rid, "path": f"{base}.no_deadline", "received": True, "code": "task.cross_field",
                        "rule": "`deadline` and `no_deadline` exclude each other: a row names a due date or "
                                "clears it, not both",
                        "fix": "keep `deadline` when the candidate's lines name a date or an event it is needed "
                               "for; otherwise keep `no_deadline` and leave `deadline` out"})
        return out

    def example(self, item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        return row

    def example_item(self, row: dict[str, Any]) -> dict[str, Any]:
        return {}


__all__ = ["ReviewCheck"]
