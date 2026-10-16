"""The record index row (TD §4.3). The raw body lives in ``blobs/``; records are immutable."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Record(BaseModel):
    record_id: str
    source: str
    unit: str
    generation: int
    query: dict[str, Any]
    interval: dict[str, Any]
    page: int
    outcome: str
    blob: str
    received_at: str
