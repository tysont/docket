"""The destination interface (TD §11.1) and its data models.

A destination turns an ``Action`` into fully concrete ``Operation`` rows
(``preview``), performs one (``apply``, idempotent by ``op_id``) and reads it
back (``verify``). ``snapshot`` returns a ``DestState`` whose digest changes
whenever anything an operation could depend on changes. ``sandbox`` returns a
twin: the same adapter class over a swapped I/O layer, so a rehearsal runs the
code a live run will run.

Refusals are integrity errors with a ``dest.*`` code; the executor records
them per operation and continues with operations that do not depend on them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from docket.cli.errors import IntegrityError
from docket.config.digest import digest

# Action kinds a destination may accept (FR-ACT-01). Config options name the first of each family.
ACTION_KINDS = (
    "task.add", "task.update", "task.close", "task.carry",
    "agenda.add",
    "calendar.place", "calendar.move", "calendar.update", "calendar.release", "calendar.rekey",
    "message.draft",
    "journal.write",
)
VERBS = ("append", "replace", "create", "move", "update", "delete", "rekey", "draft", "write_section")
PROVENANCE = ("question_id", "item_id", "depends_on")


class Action(BaseModel):
    """What the operator decided, before it is rendered for a destination.

    ``fields`` is the rendering context (item fields plus answer overrides).
    ``key`` seeds the ownership key for calendar and message objects. ``target``
    names an existing object: the current text of a Markdown line, or a
    calendar ownership key or external id.
    """
    model_config = ConfigDict(extra="forbid")
    kind: str
    destination: str
    fields: dict[str, Any] = {}
    key: str | None = None
    target: str | None = None
    question_id: str | None = None
    item_id: str | None = None
    option_id: str | None = None
    depends_on: list[str] = []


class Operation(BaseModel):
    """A fully concrete change (TD §11.1). Attendees and recurrence have no field, by design (FR-ACT-04).

    Markdown: ``path``, ``anchor`` (section heading), ``old_digest`` (absent for
    an append) and ``line``. Calendar: ``calendar``, ``key`` (ownership key),
    ``start``, ``end``, ``title``, ``notes``; ``external_id`` when the plan saw
    the object; ``new_key`` for a rekey. Message: ``to``, ``subject``, ``body``,
    ``key``. ``depends_on`` lists op ids that must succeed first.
    """
    model_config = ConfigDict(extra="forbid")
    op_id: str
    destination: str
    verb: str
    # markdown
    path: str | None = None
    anchor: str | None = None
    old_digest: str | None = None
    old_line: str | None = None
    line: str | None = None
    # calendar
    calendar: str | None = None
    key: str | None = None
    new_key: str | None = None
    external_id: str | None = None
    start: str | None = None
    end: str | None = None
    title: str | None = None
    notes: str | None = None
    # message
    to: list[str] = []
    subject: str | None = None
    body: str | None = None
    # provenance and ordering (not part of op_id)
    depends_on: list[str] = []
    question_id: str | None = None
    item_id: str | None = None

    @property
    def id(self) -> str:
        return self.op_id

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=False,
                               exclude={k for k in ("to", "depends_on") if not getattr(self, k)})

    def payload(self) -> dict[str, Any]:
        return {k: v for k, v in self.dump().items() if k not in PROVENANCE and k != "op_id"}


def op_id(destination: str, verb: str, ident: str | None, payload: Mapping[str, Any]) -> str:
    """``sha256(destination, verb, ownership key or anchor, payload)``, shortened for display."""
    body = {k: v for k, v in payload.items() if k not in PROVENANCE and k not in ("op_id", "destination", "verb")
            and v not in (None, [], "")}
    return "op_" + digest([destination, verb, ident, body])[:16]


def make_op(destination: str, verb: str, *, ident: str | None, **fields: Any) -> Operation:
    clean = {k: v for k, v in fields.items() if v is not None}
    return Operation(op_id=op_id(destination, verb, ident, clean), destination=destination, verb=verb, **clean)


class Acceptance(BaseModel):
    ok: bool
    reason: str | None = None
    details: dict[str, Any] = {}


class DestState(BaseModel):
    """Digest-able current state. ``data`` is adapter-specific and is what preview reads."""
    destination: str
    adapter: str
    data: dict[str, Any] = {}

    @property
    def digest(self) -> str:
        return digest({"destination": self.destination, "adapter": self.adapter, "data": self.data})


Outcome = Literal["applied", "already_present", "refused", "failed", "blocked"]


class Receipt(BaseModel):
    op_id: str
    destination: str
    outcome: Outcome
    external_id: str | None = None
    error: dict[str, Any] | None = None


class VerifyResult(BaseModel):
    ok: bool
    status: Literal["verified", "mismatch", "missing"] = "verified"
    detail: str | None = None
    current: str | None = None


@runtime_checkable
class Destination(Protocol):
    id: str
    adapter: str
    visibility: Literal["private", "shared"]
    capabilities: frozenset[str]
    verbs: frozenset[str]
    is_twin: bool

    def can_accept(self, action: Action) -> Acceptance: ...
    def snapshot(self) -> DestState: ...
    def preview(self, action: Action, state: DestState) -> list[Operation]: ...
    def apply(self, op: Operation) -> Receipt: ...
    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult: ...
    def sandbox(self) -> "Destination": ...
    def close(self) -> None: ...


CallTool = Callable[[str, dict[str, Any]], Any]


@dataclass
class DestEnv:
    """What an adapter may use besides its own configuration."""
    root: Path                          # workspace root; relative paths resolve here
    profile: Any = None                 # profile model, for redaction and forum agendas
    date: str | None = None             # the run date (YYYY-MM-DD), for relative defaults and windows
    sandbox_dir: Path | None = None     # where Markdown twins keep their copies
    log_dir: Path | None = None         # exec-program stderr logs
    env: Mapping[str, str] = field(default_factory=dict)
    call_tool: Callable[[str], CallTool | None] | None = None  # destination id -> MCP tool caller
    window: tuple[str, str] | None = None  # calendar listing window (ISO instants)


class BaseDestination:
    """Shared plumbing: identity, capability gate, refusal helpers."""
    adapter = ""
    capabilities: frozenset[str] = frozenset()
    verbs: frozenset[str] = frozenset()

    def __init__(self, dest_id: str, cfg: Any, denv: DestEnv, *, twin: bool = False) -> None:
        self.id = dest_id
        self.cfg = cfg
        self.denv = denv
        self.visibility = cfg.visibility
        self.is_twin = twin

    # --- defaults ------------------------------------------------------------
    def can_accept(self, action: Action) -> Acceptance:
        if action.kind not in self.capabilities:
            return Acceptance(ok=False, reason=f"{self.adapter} destination {self.id!r} does not accept {action.kind}",
                              details={"capabilities": sorted(self.capabilities)})
        return Acceptance(ok=True)

    def require(self, action: Action) -> None:
        acc = self.can_accept(action)
        if not acc.ok:
            raise IntegrityError("dest.not_accepted", acc.reason or "not accepted",
                                 hint=f"docket config explain destinations.{self.id}",
                                 details={"destination": self.id, "action": action.kind, **acc.details})

    def require_verb(self, op: Operation) -> None:
        if op.verb not in self.verbs:
            raise IntegrityError("dest.unsupported", f"{self.adapter} destination {self.id!r} has no {op.verb} verb.",
                                 hint=f"docket plugin check {self.id}",
                                 details={"destination": self.id, "verb": op.verb, "verbs": sorted(self.verbs)})

    def close(self) -> None:
        return None

    def receipt(self, op: Operation, outcome: Outcome, external_id: str | None = None) -> Receipt:
        return Receipt(op_id=op.op_id, destination=self.id, outcome=outcome, external_id=external_id)
