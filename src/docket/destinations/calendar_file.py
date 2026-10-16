"""``calendar_file``: an ICS file as a calendar (TD §11.3).

Read for availability, write for testing and for systems that import ICS.
Events Docket does not own are kept byte for byte: only owned VEVENT blocks
are ever re-serialized, and only through an operation. Attendees (``ATTENDEE``)
and recurrence (``RRULE``, ``RDATE``) are read so such events are never owned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from docket.cli.errors import IntegrityError
from docket.destinations.calendar import CalendarAdapter, Event, instant
from docket.shapes.unexpected import unexpected
from docket.store.paths import write_atomic

HEADER = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Docket//Docket//EN"]


def _unescape(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            n = v[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    out = []
    while len(line.encode("utf-8")) > 75:
        cut = 75 if not out else 74
        while len(line[:cut].encode("utf-8")) > (75 if not out else 74):
            cut -= 1
        out.append(line[:cut] if not out else " " + line[:cut])
        line = line[cut:]
    out.append(line if not out else " " + line)
    return out


def _dt(params: dict[str, str], value: str, where: str) -> str:
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", v)
    if not m:
        unexpected("dest.ics", "unrecognized date-time", sample=v, reading="keep the raw value",
                   extend=f"{where}: extend calendar_file._dt")
        return v
    d, t, z = m.groups()
    dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), int(t[:2]), int(t[2:4]), int(t[4:6]))
    if z:
        dt = dt.replace(tzinfo=timezone.utc)
    elif params.get("TZID"):
        try:
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"]))
        except (ZoneInfoNotFoundError, ValueError):
            unexpected("dest.ics", "unknown TZID", sample=params["TZID"], reading="read the time as UTC",
                       extend=f"{where}: map the TZID to an IANA zone")
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ics_dt(value: str) -> str:
    n = instant(value) or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", n):
        return ";VALUE=DATE:" + n.replace("-", "")
    return ":" + n.replace("-", "").replace(":", "")


@dataclass
class Block:
    raw: list[str]                     # physical lines as read (folded), for byte-identical rewrite
    event: Event | None = None         # parsed VEVENT, or None for other content
    dirty: bool = False


@dataclass
class IcsFile:
    newline: str = "\r\n"
    blocks: list[Block] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str, where: str) -> "IcsFile":
        nl = "\r\n" if "\r\n" in text else "\n"
        phys = text.split(nl)
        if phys and phys[-1] == "":
            phys.pop()
        f = cls(newline=nl)
        cur: list[str] = []
        in_ev = False
        for ln in phys:
            if not in_ev and ln.strip().upper() == "BEGIN:VEVENT":
                if cur:
                    f.blocks.append(Block(cur))
                cur, in_ev = [ln], True
                continue
            cur.append(ln)
            if in_ev and ln.strip().upper() == "END:VEVENT":
                f.blocks.append(Block(cur, _event(cur, where)))
                cur, in_ev = [], False
        if cur:
            f.blocks.append(Block(cur))
        return f

    def render(self) -> str:
        lines: list[str] = []
        for b in self.blocks:
            lines += _serialize(b.event) if b.dirty and b.event is not None else b.raw
        return self.newline.join(lines) + self.newline

    def events(self) -> list[Event]:
        return [b.event for b in self.blocks if b.event is not None]


def _logical(raw: list[str]) -> list[str]:
    out: list[str] = []
    for ln in raw:
        if ln[:1] in (" ", "\t") and out:
            out[-1] += ln[1:]
        else:
            out.append(ln)
    return out


def _event(raw: list[str], where: str) -> Event:
    ev = Event(id="")
    for ln in _logical(raw):
        name_params, _, value = ln.partition(":")
        name, *plist = name_params.split(";")
        params = dict(p.split("=", 1) for p in plist if "=" in p)
        name = name.upper()
        if name == "UID":
            ev.id = value.strip()
        elif name == "SUMMARY":
            ev.title = _unescape(value)
        elif name == "DESCRIPTION":
            ev.notes = _unescape(value)
        elif name == "DTSTART":
            ev.start = _dt(params, value, where)
        elif name == "DTEND":
            ev.end = _dt(params, value, where)
        elif name == "ATTENDEE":
            ev.attendees += 1
        elif name in ("RRULE", "RDATE"):
            ev.recurring = True
        elif name == "TRANSP":
            ev.transparent = value.strip().upper() == "TRANSPARENT"
    if not ev.id:
        unexpected("dest.ics", "VEVENT without UID", reading="identify the event by its start and title",
                   extend=f"{where}: give every event a UID")
        ev.id = f"nouid-{ev.start}-{ev.title}"
    return ev


def _serialize(ev: Event) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{ev.id}", "DTSTAMP" + _ics_dt(ev.start), "DTSTART" + _ics_dt(ev.start),
             "DTEND" + _ics_dt(ev.end), f"SUMMARY:{_escape(ev.title)}"]
    if ev.notes:
        lines.append(f"DESCRIPTION:{_escape(ev.notes)}")
    if ev.transparent:
        lines.append("TRANSP:TRANSPARENT")
    lines.append("END:VEVENT")
    out: list[str] = []
    for ln in lines:
        out += _fold(ln)
    return out


class IcsBackend:
    name = "ics"

    def __init__(self, path: Path, where: str) -> None:
        self.path = path
        self.where = where

    def _load(self) -> IcsFile:
        if not self.path.is_file():
            return IcsFile(blocks=[Block(list(HEADER)), Block(["END:VCALENDAR"])])
        return IcsFile.parse(self.path.read_bytes().decode("utf-8"), self.where)

    def _save(self, f: IcsFile) -> None:
        write_atomic(self.path, f.render())

    def _find(self, f: IcsFile, ev: Event) -> Block:
        for b in f.blocks:
            if b.event is not None and b.event.id == ev.id:
                return b
        raise IntegrityError("dest.state_changed", f"{self.where}: event {ev.id} left the file between the listing "
                             "and the write", hint="docket plan build", details={"event": ev.id})

    def list(self, calendar: str | None, window: tuple[str, str] | None) -> list[Event]:
        out = []
        for e in self._load().events():
            if window and e.start and e.end and not (instant(e.start) < instant(window[1])
                                                     and instant(e.end) > instant(window[0])):
                continue
            out.append(e)
        return out

    def create(self, calendar: str | None, key: str, start: str, end: str, title: str, notes: str) -> str:
        f = self._load()
        uid = f"{key}@docket"
        blk = Block([], Event(id=uid, title=title, start=start, end=end, notes=notes, calendar=calendar), dirty=True)
        idx = next((i for i, b in enumerate(f.blocks) if b.event is None and
                    any(x.strip().upper() == "END:VCALENDAR" for x in b.raw)), len(f.blocks))
        tail = f.blocks[idx] if idx < len(f.blocks) else None
        if tail is not None and tail.raw[0].strip().upper() != "END:VCALENDAR":
            # a block holding other content followed by END:VCALENDAR: split it
            cut = next(i for i, x in enumerate(tail.raw) if x.strip().upper() == "END:VCALENDAR")
            f.blocks[idx:idx + 1] = [Block(tail.raw[:cut]), Block(tail.raw[cut:])]
            idx += 1
        f.blocks.insert(idx, blk)
        self._save(f)
        return uid

    def move(self, calendar: str | None, ev: Event, key: str, start: str, end: str) -> None:
        f = self._load()
        b = self._find(f, ev)
        b.event.start, b.event.end, b.dirty = start, end, True
        self._save(f)

    def update(self, calendar: str | None, ev: Event, key: str, title: str, notes: str) -> None:
        f = self._load()
        b = self._find(f, ev)
        b.event.title, b.event.notes, b.dirty = title, notes, True
        self._save(f)

    def rekey(self, calendar: str | None, ev: Event, key: str, new_key: str, notes: str) -> None:
        self.update(calendar, ev, new_key, ev.title, notes)

    def delete(self, calendar: str | None, ev: Event, key: str) -> None:
        f = self._load()
        f.blocks = [b for b in f.blocks if not (b.event is not None and b.event.id == ev.id)]
        self._save(f)

    def twin(self) -> None:
        return None

    def close(self) -> None:
        return None


class CalendarFile(CalendarAdapter):
    adapter = "calendar_file"

    def make_backend(self) -> Any:
        rel = self.cfg.path or f"{self.id}.ics"
        return IcsBackend(self.denv.root / rel, f"destinations.{self.id}")
