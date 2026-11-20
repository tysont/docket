"""The rules a ``review`` row keeps beyond its declared cross-field rules (TD §9.4).

- ``deadline`` and ``no_deadline: true`` exclude each other: one names a due date, the
  other says the item has none (``task.cross_field``).
- ``tracked`` names a handle of the pack's ``open_tasks`` (``task.unknown_ref``), and only
  with verdict ``ask`` or ``needs_verification`` (``task.cross_field``): a tracked
  candidate is still put to the operator.

Each error names the row, the path, the value received, the rule and the fix, so the agent
that wrote the row can correct it and submit again.
"""
from __future__ import annotations

from typing import Any

TRACKED_VERDICTS = ("ask", "needs_verification")


def handles(pack: dict[str, Any] | None) -> list[str]:
    """The open task handles a pack shows, in order."""
    block = (pack or {}).get("open_tasks")
    lines = block.get("lines") if isinstance(block, dict) else None
    return [str(ln.get("handle")) for ln in lines or [] if isinstance(ln, dict) and ln.get("handle")]


def _known(shown: list[str]) -> str:
    if len(shown) <= 12:
        return ", ".join(shown)
    return f"{', '.join(shown[:6])}, ..., {', '.join(shown[-3:])}"


class ReviewCheck:
    def row_errors(self, item: dict[str, Any], row: dict[str, Any], base: str,
                   pack: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rid = row["id"]
        out: list[dict[str, Any]] = []

        def err(path: str, received: Any, code: str, rule: str, fix: str) -> None:
            out.append({"id": rid, "path": f"{base}.{path}", "received": received, "code": code, "rule": rule,
                        "fix": fix})

        if row.get("no_deadline") is True and row.get("deadline") not in (None, ""):
            err("no_deadline", True, "task.cross_field",
                "`deadline` and `no_deadline` exclude each other: a row names a due date or clears it, not both",
                "keep `deadline` when the candidate's lines name a date or an event it is needed for; otherwise "
                "keep `no_deadline` and leave `deadline` out")
        tracked = row.get("tracked")
        if tracked not in (None, ""):
            shown = handles(pack)
            if tracked not in shown:
                err("tracked", tracked, "task.unknown_ref", "`tracked` must name a handle of the pack's open_tasks",
                    f"use one of: {_known(shown)}" if shown else
                    "leave `tracked` out: this pack lists no open tasks")
            if row.get("verdict") not in TRACKED_VERDICTS:
                err("tracked", tracked, "task.cross_field",
                    "`tracked` goes with verdict `ask` or `needs_verification`: a tracked candidate is still put "
                    "to the operator",
                    "make the verdict ask or needs_verification, or leave `tracked` out")
        return out

    def example(self, item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        return row

    def example_item(self, row: dict[str, Any]) -> dict[str, Any]:
        return {}


__all__ = ["ReviewCheck", "TRACKED_VERDICTS", "handles"]
