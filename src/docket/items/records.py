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
    # M2: a record is one entry of a page; ``blob`` holds the page body as received.
    entry: int | None = None
    identity: str | None = None
    item_id: str | None = None
    target: str | None = None
