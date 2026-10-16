"""Plan artifact models (TD §12.6). M1 plans carry task-line operations only."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Operation(BaseModel):
    """A fully concrete change. ``op_id`` is a digest of destination, verb, anchor and payload."""
    op_id: str
    destination: str
    verb: str
    path: str
    anchor: str
    line: str
    question_id: str | None = None
    item_id: str | None = None


class Plan(BaseModel):
    """``plan/<digest>/plan.json``; its canonical-JSON digest is the plan digest the approval binds."""
    inputs: dict[str, Any]
    operations: list[Operation]
    schedule: list[dict[str, Any]] = []
    unschedulable: list[dict[str, Any]] = []
    notes: list[str] = []
