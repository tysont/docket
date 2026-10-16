"""Calendar destinations: ownership, preview, apply and verify over a swappable backend (TD §11.2, §11.3).

Every block Docket writes carries an ownership marker rendered from
``destinations.<id>.marker`` (default ``[docket:{key}]``) on its own line at
the end of the notes. Docket lists, moves, updates and deletes only events
whose notes carry a valid marker and that have no attendees and no
recurrence; anything else is busy time it never touches. Operations have no
attendee or recurrence field at all.

The adapter logic lives in ``CalendarAdapter``. A ``Backend`` is the I/O layer:
``MemoryBackend`` (twins and tests), the ICS file backend, the exec-protocol
backend and the MCP backend. ``sandbox()`` swaps the backend for a
``MemoryBackend`` seeded from the live listing, unless the backend supplies
its own twin.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from docket.cli.errors import ConfigError, IntegrityError
from docket.config.digest import digest
from docket.destinations.base import (
    Acceptance, Action, BaseDestination, DestEnv, DestState, Operation, Receipt, VerifyResult, make_op,
)
from docket.shapes.unexpected import unexpected
from docket.text import templates
from docket.text.redact import Redactor

DEFAULT_MARKER = "[docket:{key}]"
DEFAULT_TITLE = "{title}"
DEFAULT_NOTES = "{note}"
KEY_CHARS = r"[A-Za-z0-9_-]+"


def instant(value: str | None) -> str | None:
    """Normalize an ISO 8601 timestamp to UTC; a date-only value stays a date."""
    if not value:
        return value
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return v
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Event:
    id: str
    title: str = ""
    start: str = ""
    end: str = ""
    notes: str = ""
    calendar: str | None = None
    attendees: int = 0
    recurring: bool = False
    transparent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "start": self.start, "end": self.end, "notes": self.notes,
                "calendar": self.calendar, "attendees": self.attendees, "recurring": self.recurring,
                "transparent": self.transparent}

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "calendar") -> "Event":
        """Read an event from a backend row, tolerating field variants."""
        title = d.get("title", d.get("summary", ""))
        notes = d.get("notes", d.get("description", ""))
        att = d.get("attendees", 0)
        if isinstance(att, list):
            att = len(att)
        rec = d.get("recurring", d.get("recurrence", False))
        known = {"id", "title", "summary", "start", "end", "notes", "description", "calendar", "attendees",
                 "recurring", "recurrence", "transparent", "key"}
        extra = sorted(set(d) - known)
        if extra:
            unexpected("dest.calendar", "event has unmodelled fields", sample=extra[:10],
                       reading="ignore the fields", extend=f"{where}: map the fields in Event.from_dict")
        return cls(id=str(d.get("id", "")), title=str(title or ""), start=str(d.get("start", "")),
                   end=str(d.get("end", "")), notes=str(notes or ""), calendar=d.get("calendar"),
                   attendees=int(att or 0), recurring=bool(rec), transparent=bool(d.get("transparent", False)))


class Backend(Protocol):
    name: str

    def list(self, calendar: str | None, window: tuple[str, str] | None) -> list[Event]: ...
    def create(self, calendar: str | None, key: str, start: str, end: str, title: str, notes: str) -> str: ...
    def move(self, calendar: str | None, ev: Event, key: str, start: str, end: str) -> None: ...
    def update(self, calendar: str | None, ev: Event, key: str, title: str, notes: str) -> None: ...
    def delete(self, calendar: str | None, ev: Event, key: str) -> None: ...
    def rekey(self, calendar: str | None, ev: Event, key: str, new_key: str, notes: str) -> None: ...
    def twin(self) -> "Backend | None": ...
    def close(self) -> None: ...


class MemoryBackend:
    """An in-memory calendar: the generic twin, and the test double for every calendar adapter."""
    name = "memory"

    def __init__(self, events: list[Event] | None = None) -> None:
        self.events: dict[str, Event] = {e.id: copy.copy(e) for e in (events or [])}
        self._n = 0

    def _next_id(self) -> str:
        self._n += 1
        while f"mem-{self._n}" in self.events:
            self._n += 1
        return f"mem-{self._n}"

    def list(self, calendar: str | None, window: tuple[str, str] | None) -> list[Event]:
        out = []
        for e in self.events.values():
            if window and e.start and e.end and not (instant(e.start) < instant(window[1])
                                                     and instant(e.end) > instant(window[0])):
                continue
            out.append(copy.copy(e))
        return sorted(out, key=lambda e: (instant(e.start) or "", e.id))

    def create(self, calendar: str | None, key: str, start: str, end: str, title: str, notes: str) -> str:
        eid = self._next_id()
        self.events[eid] = Event(id=eid, title=title, start=start, end=end, notes=notes, calendar=calendar)
        return eid

    def move(self, calendar: str | None, ev: Event, key: str, start: str, end: str) -> None:
        e = self.events[ev.id]
        e.start, e.end = start, end

    def update(self, calendar: str | None, ev: Event, key: str, title: str, notes: str) -> None:
        e = self.events[ev.id]
        e.title, e.notes = title, notes

    def delete(self, calendar: str | None, ev: Event, key: str) -> None:
        self.events.pop(ev.id, None)

    def rekey(self, calendar: str | None, ev: Event, key: str, new_key: str, notes: str) -> None:
        self.events[ev.id].notes = notes

    def twin(self) -> None:
        return None

    def close(self) -> None:
        return None


# --- markers ----------------------------------------------------------------------


class Marker:
    """Renders and parses the ownership marker template. Only ``{key}`` and ``{destination}`` are fields."""

    def __init__(self, template: str | None, dest_id: str) -> None:
        self.template = template or DEFAULT_MARKER
        self.dest_id = dest_id
        parts = []
        for nd in templates.parse(self.template).nodes:
            if isinstance(nd, templates.Text):
                parts.append(re.escape(nd.text))
            elif isinstance(nd, templates.Var) and nd.path == "key":
                parts.append(f"(?P<key>{KEY_CHARS})")
            elif isinstance(nd, templates.Var) and nd.path == "destination":
                parts.append(re.escape(dest_id))
            else:
                raise ConfigError("config.bad_template", "a marker template may use only {key} and {destination}",
                                  hint=f"docket config explain destinations.{dest_id}.marker")
        if "(?P<key>" not in "".join(parts):
            raise ConfigError("config.bad_template", "a marker template must contain {key}",
                              hint=f"docket config explain destinations.{dest_id}.marker")
        self.rx = re.compile("".join(parts))

    def render(self, key: str) -> str:
        return templates.render(self.template, {"key": key, "destination": self.dest_id})

    def find(self, notes: str) -> str | None:
        keys = {m.group("key") for m in self.rx.finditer(notes or "")}
        return next(iter(keys)) if len(keys) == 1 else None

    def strip(self, notes: str) -> str:
        return self.rx.sub("", notes or "").rstrip()

    def attach(self, notes: str, key: str) -> str:
        body = self.strip(notes)
        return f"{body}\n\n{self.render(key)}" if body else self.render(key)


def ownership_key(dest_id: str, seed: str) -> str:
    return "dk_" + digest([dest_id, seed])[:12]


def owned_key(marker: Marker, notes: str, *, attendees: int = 0, recurring: bool = False,
              dest_id: str = "calendar") -> str | None:
    """The ownership key an event carries, or None when Docket does not own it (TD §11.2).

    The one implementation of ownership: the destination's adapter (apply,
    verify, snapshot) and planning (classification, scheduling, reconcile, mirrors)
    both read events through it, so a block apply creates is the block the
    next run's planning finds as owned. A marker on an event with attendees or
    recurrence is never ownership.
    """
    key = marker.find(notes)
    if key is None:
        return None
    if attendees or recurring:
        unexpected("dest.calendar", "marker on an event with attendees or recurrence",
                   reading="treat the event as not owned", extend=f"destinations.{dest_id}.marker")
        return None
    return key


def marker_for(destinations: dict[str, Any], dest_id: str) -> Marker:
    """The ownership marker of a configured destination (the default marker when it is not configured)."""
    cfg = destinations.get(dest_id)
    return Marker(getattr(cfg, "marker", None), dest_id)


def key_is_markable(key: str) -> bool:
    """Whether a key survives a round trip through any marker (the characters a marker parses)."""
    return re.fullmatch(KEY_CHARS, key or "") is not None


# --- the adapter ---------------------------------------------------------------------


class CalendarAdapter(BaseDestination):
    capabilities = frozenset({"calendar.place", "calendar.move", "calendar.update", "calendar.release",
                              "calendar.rekey"})
    verbs = frozenset({"create", "move", "update", "delete", "rekey"})

    def __init__(self, dest_id: str, cfg: Any, denv: DestEnv, *, backend: Any = None, twin: bool = False) -> None:
        super().__init__(dest_id, cfg, denv, twin=twin)
        self.backend = backend if backend is not None else self.make_backend()
        self.marker = Marker(cfg.marker, dest_id)
        self._redactor: Redactor | None = None
        self.declared_verbs = self.backend_verbs()
        self.verbs = frozenset(self.declared_verbs)
        caps = {"calendar.place"} if {"create"} <= self.verbs else set()
        caps |= {k for k, v in (("calendar.move", "move"), ("calendar.update", "update"),
                                ("calendar.release", "delete"), ("calendar.rekey", "rekey")) if v in self.verbs}
        self.capabilities = frozenset(caps)

    # adapters override these two
    def make_backend(self) -> Any:
        return MemoryBackend()

    def backend_verbs(self) -> set[str]:
        return set(type(self).verbs)

    @property
    def calendar(self) -> str | None:
        return self.cfg.calendar

    @property
    def redactor(self) -> Redactor:
        if self._redactor is None:
            self._redactor = Redactor.from_config(self.denv.profile, self.cfg.redact)
        return self._redactor

    def sandbox(self) -> "CalendarAdapter":
        tw = self.backend.twin()
        if tw is None:
            tw = MemoryBackend(self.backend.list(self.calendar, self.denv.window))
        twin = type(self)(self.id, self.cfg, self.denv, backend=tw, twin=True)
        twin.declared_verbs, twin.verbs, twin.capabilities = self.declared_verbs, self.verbs, self.capabilities
        return twin

    def close(self) -> None:
        self.backend.close()

    # --- ownership ---------------------------------------------------------------
    def owner_key(self, ev: Event) -> str | None:
        return owned_key(self.marker, ev.notes, attendees=ev.attendees, recurring=ev.recurring, dest_id=self.id)

    def listing(self) -> tuple[dict[str, Event], list[Event]]:
        owned: dict[str, Event] = {}
        other: list[Event] = []
        for ev in self.backend.list(self.calendar, self.denv.window):
            k = self.owner_key(ev)
            if k is None or k in owned:
                if k is not None:
                    unexpected("dest.calendar", "two events carry the same ownership key",
                               reading="treat the later event as not owned", extend=f"destinations.{self.id}.marker")
                other.append(ev)
            else:
                owned[k] = ev
        return owned, other

    def snapshot(self) -> DestState:
        owned, other = self.listing()
        return DestState(destination=self.id, adapter=self.adapter, data={
            "owned": {k: self._norm(e) for k, e in sorted(owned.items())},
            "busy": sorted([[instant(e.start), instant(e.end), e.id] for e in other if not e.transparent]),
            "unowned_ids": sorted(e.id for e in other),
        })

    def _norm(self, e: Event) -> dict[str, Any]:
        return {"id": e.id, "title": e.title, "start": instant(e.start), "end": instant(e.end),
                "notes": self.marker.strip(e.notes)}

    # --- rendering ---------------------------------------------------------------
    def render(self, ctx: dict[str, Any]) -> tuple[str, str]:
        tt = self.cfg.title_template or DEFAULT_TITLE
        nt = self.cfg.notes_template or DEFAULT_NOTES
        if self.visibility == "shared":
            r = self.redactor
            title, notes = r.render_title(tt, ctx), r.render_text(nt, ctx)
            r.check(title, where=f"destinations.{self.id}.title")
            r.check(notes, where=f"destinations.{self.id}.notes")
            return title, notes
        return templates.render(tt, ctx), templates.render(nt, ctx)

    # --- preview -------------------------------------------------------------------
    def can_accept(self, action: Action) -> Acceptance:
        acc = super().can_accept(action)
        if not acc.ok and action.kind.startswith("calendar."):
            acc.reason = f"{self.id}: the calendar does not declare the verb {action.kind} needs"
        return acc

    def _target(self, action: Action, state: DestState) -> tuple[str, dict[str, Any]]:
        owned = state.data.get("owned") or {}
        t = action.target or (ownership_key(self.id, action.key) if action.key else None)
        if t in owned:
            return t, owned[t]
        for k, row in owned.items():
            if row["id"] == t:
                return k, row
        if t in (state.data.get("unowned_ids") or []):
            raise self.unowned(t, "the event carries no ownership marker for this destination")
        raise IntegrityError("dest.state_changed", f"{self.id}: no owned event matches {t!r}", hint="docket plan build",
                             details={"destination": self.id, "target": t})

    def unowned(self, ref: str | None, why: str) -> IntegrityError:
        return IntegrityError("dest.unowned", f"{self.id}: {why}; Docket changes only events it owns.",
                              hint=f"docket config explain destinations.{self.id}.marker",
                              details={"destination": self.id, "target": ref})

    def preview(self, action: Action, state: DestState, **_: Any) -> list[Operation]:
        self.require(action)
        ctx = action.fields
        prov = {"question_id": action.question_id, "item_id": action.item_id, "depends_on": action.depends_on or None}
        cal = self.calendar
        if action.kind == "calendar.place":
            if not action.key:
                raise IntegrityError("dest.not_accepted", f"{self.id}: a calendar block needs a key seed",
                                     hint="docket plan build", details={"destination": self.id})
            key = ownership_key(self.id, action.key)
            start, end = ctx.get("start"), ctx.get("end")
            if not start or not end:
                raise IntegrityError("dest.not_accepted", f"{self.id}: a calendar block needs a start and an end",
                                     hint="docket plan build", details={"destination": self.id, "key": key})
            title, notes = self.render(ctx)
            cur = (state.data.get("owned") or {}).get(key)
            if cur is None:
                return [make_op(self.id, "create", ident=key, calendar=cal, key=key, start=start, end=end,
                                title=title, notes=notes, **prov)]
            ops = []
            if (cur["start"], cur["end"]) != (instant(start), instant(end)):
                ops.append(make_op(self.id, "move", ident=key, calendar=cal, key=key, external_id=cur["id"],
                                   start=start, end=end, **prov))
            if (cur["title"], cur["notes"]) != (title, notes.rstrip()):
                ops.append(make_op(self.id, "update", ident=key, calendar=cal, key=key, external_id=cur["id"],
                                   title=title, notes=notes, **prov))
            return ops
        key, cur = self._target(action, state)
        if action.kind == "calendar.move":
            return [make_op(self.id, "move", ident=key, calendar=cal, key=key, external_id=cur["id"],
                            start=ctx.get("start"), end=ctx.get("end"), **prov)]
        if action.kind == "calendar.update":
            title, notes = self.render({**ctx, "title": ctx.get("title") or cur["title"]})
            return [make_op(self.id, "update", ident=key, calendar=cal, key=key, external_id=cur["id"],
                            title=title, notes=notes, **prov)]
        if action.kind == "calendar.release":
            return [make_op(self.id, "delete", ident=key, calendar=cal, key=key, external_id=cur["id"], **prov)]
        new_key = ownership_key(self.id, str(ctx.get("new_seed") or ctx.get("new_key") or ""))
        return [make_op(self.id, "rekey", ident=key, calendar=cal, key=key, new_key=new_key,
                        external_id=cur["id"], **prov)]

    # --- apply ------------------------------------------------------------------------
    def _resolve(self, op: Operation) -> tuple[dict[str, Event], Event | None]:
        owned, other = self.listing()
        ev = owned.get(op.key or "")
        if ev is None and op.external_id:
            if any(o.id == op.external_id for o in other):
                raise self.unowned(op.external_id, "the target event carries no valid ownership marker")
            if any(o.id == op.external_id and k != op.new_key for k, o in owned.items()):
                raise self.unowned(op.external_id, "the target event carries a different ownership key")
        return owned, ev

    def apply(self, op: Operation) -> Receipt:
        self.require_verb(op)
        owned, ev = self._resolve(op)
        cal = op.calendar
        if op.verb == "create":
            notes = self.marker.attach(op.notes or "", op.key)
            if ev is not None:
                if self._same(ev, op):
                    return self.receipt(op, "already_present", ev.id)
                raise IntegrityError("dest.state_changed", f"{self.id}: an owned event with key {op.key} already "
                                     "exists with different content", hint="docket plan build",
                                     details={"destination": self.id, "key": op.key, "current": self._norm(ev)})
            eid = self.backend.create(cal, op.key, op.start, op.end, op.title or "", notes)
            return self.receipt(op, "applied", eid)
        if op.verb == "delete":
            if ev is None:
                return self.receipt(op, "already_present", op.external_id)
            self.backend.delete(cal, ev, op.key)
            return self.receipt(op, "applied", ev.id)
        if op.verb == "rekey":
            if ev is None:
                if op.new_key in owned:
                    return self.receipt(op, "already_present", owned[op.new_key].id)
                raise self._missing(op)
            self.backend.rekey(cal, ev, op.key, op.new_key, self.marker.attach(ev.notes, op.new_key))
            return self.receipt(op, "applied", ev.id)
        if ev is None:
            raise self._missing(op)
        if op.verb == "move":
            if (instant(ev.start), instant(ev.end)) == (instant(op.start), instant(op.end)):
                return self.receipt(op, "already_present", ev.id)
            self.backend.move(cal, ev, op.key, op.start, op.end)
            return self.receipt(op, "applied", ev.id)
        notes = self.marker.attach(op.notes or "", op.key)
        if ev.title == (op.title or "") and self.marker.strip(ev.notes) == self.marker.strip(notes):
            return self.receipt(op, "already_present", ev.id)
        self.backend.update(cal, ev, op.key, op.title or "", notes)
        return self.receipt(op, "applied", ev.id)

    def _missing(self, op: Operation) -> IntegrityError:
        return IntegrityError("dest.state_changed", f"{self.id}: the owned event {op.key} no longer exists",
                              hint="docket plan build", details={"destination": self.id, "key": op.key})

    def _same(self, ev: Event, op: Operation) -> bool:
        return (instant(ev.start), instant(ev.end), ev.title, self.marker.strip(ev.notes)) == \
            (instant(op.start), instant(op.end), op.title or "", (op.notes or "").rstrip())

    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult:
        owned, _ = self.listing()
        if op.verb == "delete":
            ok = op.key not in owned
            return VerifyResult(ok=ok, status="verified" if ok else "mismatch", detail=None if ok else "still present")
        key = op.new_key if op.verb == "rekey" else op.key
        ev = owned.get(key or "")
        if ev is None:
            return VerifyResult(ok=False, status="missing", detail=f"no owned event with key {key}")
        if op.verb == "create" and not self._same(ev, op):
            return VerifyResult(ok=False, status="mismatch", current=str(self._norm(ev)))
        if op.verb == "move" and (instant(ev.start), instant(ev.end)) != (instant(op.start), instant(op.end)):
            return VerifyResult(ok=False, status="mismatch", current=str(self._norm(ev)))
        if op.verb == "update" and ev.title != (op.title or ""):
            return VerifyResult(ok=False, status="mismatch", current=str(self._norm(ev)))
        return VerifyResult(ok=True)


def events_equal(a: list[Event], b: list[Event]) -> bool:
    def n(e: Event) -> tuple:
        return (e.title, instant(e.start), instant(e.end), e.notes, e.attendees, e.recurring)
    return sorted(map(n, a)) == sorted(map(n, b))

