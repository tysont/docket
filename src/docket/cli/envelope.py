"""The output envelope (TD §15.2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from docket.engine.steps import Command


class NextHint(BaseModel):
    command: list[str]
    shell: str
    why: str


class ErrorBody(BaseModel):
    code: str
    kind: str
    message: str
    hint: str | None = None
    retryable: bool = False
    details: dict[str, Any] = {}


class WarningRow(BaseModel):
    code: str
    message: str


class UnexpectedRow(BaseModel):
    area: str
    detail: str
    reading: str
    extend: str
    sample: str | None = None


class Envelope(BaseModel):
    """Every command's JSON output. On failure ``ok`` is false and ``error`` replaces ``data``."""
    ok: bool
    command: str | None
    workspace: str | None = None
    run: str | None = None
    data: dict[str, Any] | None = None
    error: ErrorBody | None = None
    warnings: list[WarningRow] = []
    unexpected: list[UnexpectedRow] = []
    next: NextHint | None = None


@dataclass
class Result:
    """What a command handler returns."""
    data: dict[str, Any]
    summary: str
    next: tuple[list[str], str] | None = None  # (argv, why)
    warnings: list[dict[str, str]] = field(default_factory=list)


def next_hint(argv: list[str] | None, why: str) -> dict[str, Any] | None:
    if not argv:
        return None
    return {"command": list(argv), "shell": Command.of(argv).shell, "why": why}
