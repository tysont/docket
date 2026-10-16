"""Recording unexpected shapes (TD §14.2).

A shape check never refuses. It takes the safe reading, continues, and calls
``unexpected()`` naming where to extend the model. Records collect in a
context-bound collector that the CLI drains into the envelope, the run's
``unexpected.jsonl`` and the database.
"""
from __future__ import annotations

import contextvars
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterator
from contextlib import contextmanager

MAX_SAMPLE_BYTES = 500


@dataclass(frozen=True)
class Shape:
    area: str
    detail: str
    reading: str
    extend: str
    sample: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["sample"] is None:
            del d["sample"]
        return d


class Collector:
    def __init__(self) -> None:
        self._rows: list[Shape] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, shape: Shape) -> None:
        key = (shape.area, shape.detail)
        if key in self._seen:
            return
        self._seen.add(key)
        self._rows.append(shape)

    def drain(self) -> list[Shape]:
        rows, self._rows, self._seen = self._rows, [], set()
        return rows

    def peek(self) -> list[Shape]:
        return list(self._rows)


_current: contextvars.ContextVar[Collector | None] = contextvars.ContextVar("docket_unexpected", default=None)


@contextmanager
def collecting() -> Iterator[Collector]:
    col = Collector()
    token = _current.set(col)
    try:
        yield col
    finally:
        _current.reset(token)


def _sample_text(sample: Any) -> str | None:
    if sample is None:
        return None
    text = sample if isinstance(sample, str) else json.dumps(sample, sort_keys=True, default=str)
    raw = text.encode("utf-8")
    if len(raw) > MAX_SAMPLE_BYTES:
        text = raw[:MAX_SAMPLE_BYTES].decode("utf-8", "ignore")
    return text


def unexpected(area: str, detail: str, *, sample: Any = None, reading: str, extend: str) -> Shape:
    """Record a shape the code has not modelled. ``extend`` must name where to model it."""
    if not area or not detail:
        raise ValueError("unexpected() needs an area and a detail")
    if not reading or not reading.strip():
        raise ValueError("unexpected() needs the safe reading taken")
    if not extend or not extend.strip():
        raise ValueError("unexpected() needs a non-empty extend naming the extension point")
    shape = Shape(area=area, detail=detail, reading=reading, extend=extend, sample=_sample_text(sample))
    col = _current.get()
    if col is not None:
        col.add(shape)
    return shape
