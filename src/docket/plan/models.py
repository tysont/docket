"""Plan artifact models (TD §12.6)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from docket.destinations.base import Operation  # the concrete operation model (TD §11.1)


class Plan(BaseModel):
    """``plan/<digest>/plan.json``; its canonical-JSON digest is the plan digest the approval binds."""
    inputs: dict[str, Any]
    operations: list[Operation]
    schedule: list[dict[str, Any]] = []
    unschedulable: list[dict[str, Any]] = []
    notes: list[str] = []
