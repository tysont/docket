"""The source port (TD §6.2) and the collection data model.

- ``Unit``: one discovered subdivision of a source (a space, a queue, a folder).
- ``Cursor``: where a read starts and what interval it covers.
- ``Entry``: one element of a page, as received and as read (variations applied).
- ``Page``: one transport response and its entries.
- ``Outcome``: how a source or a unit ended (TD §6.6).

Outcomes: ``read``, ``empty``, ``partial(position)``, ``failed(cause)``,
``unavailable(reason, instructions)``, ``skipped(policy)``. A cause keeps the
system's own message and status verbatim.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

Status = Literal["read", "empty", "partial", "failed", "unavailable", "skipped"]
ALL_UNITS = "*"


@dataclass
class Unit:
    id: str
    name: str | None = None
    type: str | None = None
    members: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Cursor:
    model: str
    position: str | None = None        # where reading starts (durable cursor, or a refresh start)
    coverage_end: str | None = None
    interval_from: str | None = None
    interval_to: str | None = None
    start_generation: int | None = None  # the generation the start position came from (refresh)


@dataclass
class Entry:
    index: int
    raw: Any
    view: dict[str, Any] | None         # None: the record failed its contract or is not an object
    tombstone: bool = False
    order_key: Any = None


@dataclass
class Page:
    number: int
    request: dict[str, Any]
    body: bytes
    entries: list[Entry]
    next_token: str | None
    end: bool
    page_size: int


@dataclass
class Outcome:
    status: Status
    position: str | None = None         # partial: the position to retry from
    cause: dict[str, Any] | None = None  # failed: {code, message, status, ...}
    reason: str | None = None           # unavailable, skipped, partial
    instructions: str | None = None     # unavailable
    entries: int = 0
    pages: int = 0
    stepdowns: int = 0
    page_size: int | None = None
    tombstones: int = 0
    failed_records: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, 0) or k in ("status", "entries")}


@dataclass
class AuthStatus:
    ok: bool
    reason: str | None = None
    instructions: str | None = None


@dataclass
class Resolution:
    source: str
    ref: str
    state: str | None = None
    last_activity: str | None = None
    last_actor: str | None = None
    title: str | None = None
    status: Literal["resolved", "unresolvable", "failed"] = "resolved"
    cause: dict[str, Any] | None = None


class UnitRead(Protocol):
    """An async iterator over a unit's pages; ``outcome`` and ``proposal`` are set when it ends."""
    outcome: Outcome | None
    proposal: tuple[str | None, str | None, bool]

    def __aiter__(self) -> AsyncIterator[Page]: ...


class Source(Protocol):
    id: str
    capabilities: set[str]

    async def discover(self, ctx: Any) -> list[Unit] | Outcome: ...
    def read(self, ctx: Any, unit: Unit | None, cursor: Cursor) -> UnitRead: ...
    async def resolve(self, ctx: Any, ref: str) -> Resolution | None: ...
    async def check_auth(self, ctx: Any) -> AuthStatus: ...
