"""The stored answer (TD §10.3), keyed by question id in ``answers.json``."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Answer(BaseModel):
    option: str
    action: str | None = None
    overrides: dict[str, Any] = {}
    digest: str | None = None
    input: str | None = None
