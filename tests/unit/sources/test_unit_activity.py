"""Quiet units (TD §6.9): a source in thousands of units reads only the ones that moved.

Discovery may report each unit's last activity. A unit whose last activity is not after
the place its cursor starts has nothing new and is not read, as long as it was really
read within ``discover.verify_every``. Past that window it is read anyway, and a record
found in a unit the rule would have skipped is reported: the activity time was wrong.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from docket.config.source_models import Source
from docket.shapes.unexpected import collecting
from docket.sources.collect import Starts, _read_one
from docket.sources.declarative import DeclarativeSource, Limits
from docket.transports.base import Response, Secrets

pytestmark = pytest.mark.smoke
INSTANT = "2026-10-15T18:00:00+00:00"
YESTERDAY = "2026-10-14T18:00:00+00:00"
LONG_AGO = "2026-10-01T18:00:00+00:00"


class Rooms:
    """Units with a last-activity time and messages. Records every request."""
    kind, id, info = "scripted", "t", {}

    def __init__(self, units: dict[str, tuple[str | None, list[str]]], *, cut_above: int | None = None):
        self.units = units  # id -> (last activity, message timestamps)
        self.cut_above = cut_above  # a discovery page above this many units comes back cut, with a note
        self.requests: list[dict] = []

    async def open(self):
        return None

    async def close(self):
        return None

    async def request(self, req):
        p = req.payload
        self.requests.append(p)
        a = p.get("args", {})
        if p["op"] == "discover":
            ids = sorted(self.units)
            size = int(a.get("size") or 2)
            start = int(a.get("token") or 0)
            chunk = ids[start:start + size]
            nxt = start + len(chunk)
            rows = [{"id": i, "name": i.upper(), **({"active": self.units[i][0]} if self.units[i][0] else {})}
                    for i in chunk]
            if self.cut_above is not None and size > self.cut_above:
                whole = json.dumps({"units": rows, "next": None})
                return Response(body=(whole[:len(whole) // 2] + "\n--- TRUNCATED ---\nResponse was too long").encode())
            return Response(body=json.dumps({"units": rows, "next": str(nxt) if nxt < len(ids) else None}).encode())
        after = a.get("after")
        rows = [{"id": f"{a['unit']}-{n}", "ts": ts} for n, ts in enumerate(self.units[a["unit"]][1])
                if after is None or ts > after]
        return Response(body=json.dumps({"items": rows, "next": None}).encode())

    def read_units(self) -> list[str]:
        return sorted(p["args"]["unit"] for p in self.requests if p["op"] == "read")


def source(**discover):
    return Source.model_validate({
        "transport": "t", "cursor": {"model": "units", "lookback": "1d"},
        "request": {"discover": {"op": "discover", "args": {"token": "{page.token}", "size": "{page.size}"}},
                    "read": {"op": "read", "args": {"unit": "{unit.id}", "after": "{cursor.start}"}}},
        "discover": {"entries": "$.units", "next": "$.next", "id": "$.id", "name": "$.name", "type": None,
                     "active": "$.active", **discover},
        "paging": {"entries": "$.items", "next": "$.next", "order_key": "$.ts"},
        "contract": {"entry": {"required": {"id": "str", "ts": "timestamp"}}}})


def collect(cfg, server, durable=None, *, entry_limit=None, only_unit=None):
    async def go():
        src = DeclarativeSource("chat", cfg, server, secrets=Secrets(),
                                limits=Limits(asyncio.Semaphore(4), asyncio.Semaphore(4)), env={})
        starts = Starts(durable={("chat", u): v for u, v in (durable or {}).items()})
        return await _read_one(src, "chat", cfg, starts=starts, instant=INSTANT, run_date=date(2026, 10, 15),
                               entry_limit=entry_limit, only_unit=only_unit)
    with collecting() as col:
        res = asyncio.run(go())
        return res, col.drain()


BUSY = ("2026-10-15T09:00:00+00:00", ["2026-10-15T08:00:00+00:00", "2026-10-15T09:00:00+00:00"])
QUIET = ("2026-10-10T09:00:00+00:00", ["2026-10-10T09:00:00+00:00"])


def test_a_unit_read_recently_with_no_activity_since_its_cursor_is_not_read():
    server = Rooms({"busy": BUSY, "quiet": QUIET, "still": QUIET})
    durable = {"busy": ("2026-10-14T12:00:00+00:00", YESTERDAY),      # position, coverage end
               "quiet": ("2026-10-10T09:00:00+00:00", YESTERDAY),     # at its last message
               "still": (None, YESTERDAY)}                             # read before, held nothing
    res, shapes = collect(source(), server, durable)
    assert server.read_units() == ["busy"]
    assert [u.unit.id for u in res.units] == ["busy"] and res.quiet == ["quiet", "still"]
    assert res.outcome.status == "read" and res.outcome.entries == 2 and shapes == []
    assert len(res.discovered) == 3  # the registry still learns every unit


def test_a_unit_never_read_is_read_once_so_its_cursor_exists():
    server = Rooms({"old": QUIET})
    res, _ = collect(source(), server)
    assert server.read_units() == ["old"] and res.quiet == []
    assert res.units[0].outcome.status == "empty" and res.units[0].proposal[1] == INSTANT  # coverage proven to now


def test_past_the_verify_window_a_quiet_unit_is_read_and_a_wrong_activity_time_is_reported():
    wrong = ("2026-10-10T09:00:00+00:00", ["2026-10-10T09:00:00+00:00", "2026-10-12T07:00:00+00:00"])
    server = Rooms({"honest": QUIET, "wrong": wrong})
    durable = {"honest": ("2026-10-10T09:00:00+00:00", LONG_AGO), "wrong": ("2026-10-10T09:00:00+00:00", LONG_AGO)}
    res, shapes = collect(source(verify_every="7d"), server, durable)
    assert server.read_units() == ["honest", "wrong"] and res.quiet == []
    assert [(s.area, s.detail) for s in shapes] == [
        ("source.unit_activity", "chat unit wrong held 1 record(s) newer than its reported last activity")]
    assert "discover.active" in shapes[0].extend


def test_without_a_verify_window_the_activity_time_is_always_trusted():
    server = Rooms({"quiet": QUIET})
    res, _ = collect(source(verify_every=None), server, {"quiet": ("2026-10-10T09:00:00+00:00", LONG_AGO)})
    assert server.read_units() == [] and res.quiet == ["quiet"] and res.outcome.status == "empty"


def test_a_unit_with_no_activity_time_is_read():
    server = Rooms({"untimed": (None, ["2026-10-15T08:00:00+00:00"])})
    res, _ = collect(source(), server, {"untimed": (None, YESTERDAY)})
    assert server.read_units() == ["untimed"] and res.outcome.entries == 1


def test_discovery_asks_for_its_own_page_size():
    server = Rooms({f"u{i}": QUIET for i in range(5)})
    durable = {f"u{i}": (None, YESTERDAY) for i in range(5)}
    collect(source(page_size=5), server, durable)
    assert [p["args"]["size"] for p in server.requests if p["op"] == "discover"] == [5]
    server = Rooms({f"u{i}": QUIET for i in range(5)})
    collect(source(), server, durable)
    assert all("size" not in p["args"] for p in server.requests if p["op"] == "discover")  # the system's default


def test_discovery_steps_down_from_a_cut_page_and_still_lists_every_unit():
    server = Rooms({f"u{i:02d}": QUIET for i in range(12)}, cut_above=5)
    cfg = source(page_size=16)
    cfg.paging.too_large = ["truncat"]
    res, shapes = collect(cfg, server, {f"u{i:02d}": (None, YESTERDAY) for i in range(12)})
    sizes = [p["args"]["size"] for p in server.requests if p["op"] == "discover"]
    assert sizes[:3] == [16, 8, 4] and set(sizes[2:]) == {4}
    assert len(res.discovered) == 12 and shapes == []


def test_a_discovery_page_that_is_cut_at_the_smallest_size_fails_the_source_with_that_reason():
    server = Rooms({f"u{i:02d}": QUIET for i in range(12)}, cut_above=0)  # even one unit is too much
    cfg = source(page_size=4)
    cfg.paging.too_large = ["truncat"]
    res, _ = collect(cfg, server)
    assert res.outcome.status == "failed" and res.outcome.cause["code"] == "source.truncated"


def test_a_sample_stops_reading_units_once_it_has_enough_and_starts_with_the_most_active():
    units = {f"u{i}": (f"2026-10-15T0{i}:00:00+00:00", [f"2026-10-15T0{i}:00:00+00:00"]) for i in range(8)}
    server = Rooms(units)
    res, _ = collect(source(), server, entry_limit=2)
    assert server.read_units() == ["u6", "u7"]
    assert res.outcome.status == "partial" and "6 unit(s) not read" in res.outcome.reason


def test_a_named_unit_is_read_even_when_quiet():
    server = Rooms({"quiet": QUIET, "busy": BUSY})
    durable = {"quiet": ("2026-10-10T09:00:00+00:00", YESTERDAY)}
    collect(source(), server, durable, only_unit="quiet")
    assert server.read_units() == ["quiet"]


def test_verify_every_must_be_a_duration(tmp_path):
    from docket.config import check
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docket.yaml").write_text("""\
version: 1
workflows:
  daily: { stages: [collect, close] }
profile:
  timezone: UTC
  me: { name: Dana Reyes }
transports:
  gw: { kind: codemode, command: [gateway-bin], tool: run_code }
sources:
  chat:
    extends: mcp/chat-codemode
    transport: gw
    params: { spaces_tool: list_rooms, messages_tool: list_posts }
    discover: { verify_every: weekly }
""", encoding="utf-8")
    _, issues, _ = check(root)
    assert [(i.code, i.path) for i in issues] == [("config.bad_type", "sources.chat.discover.verify_every")]
