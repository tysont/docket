"""Cursor models and the pager (TD §6.4, §6.5): coverage is proven from what was read.

The case the predecessor got wrong is first: a page with fewer rows than requested
is complete only when the contract's end-of-data signal says so.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date

import pytest

from docket.config.source_models import Source
from docket.shapes.unexpected import collecting
from docket.sources.base import ALL_UNITS, Cursor, Unit
from docket.sources.cursors import apply_proposals, cadence_skip, compare, write_proposal
from docket.sources.declarative import DeclarativeSource, Limits
from docket.store.db import open_db
from docket.transports.base import Failure, Response, Secrets

pytestmark = pytest.mark.smoke
INSTANT = "2026-10-15T18:00:00+00:00"
ROWS = [{"id": f"r{i:02d}", "ts": f"2026-10-15T{i:02d}:00:00+00:00"} for i in range(10)]


class Server:
    """Pages ROWS by offset token. ``short`` returns fewer rows than asked on every page;
    ``max_rows`` declares pages above it too large; ``fail_at`` fails that request number."""
    kind, id, info = "scripted", "t", {}

    def __init__(self, rows=ROWS, *, short=False, max_rows=None, fail_at=None, end_flag=False, empty_end=False):
        self.rows, self.short, self.max_rows, self.fail_at = rows, short, max_rows, fail_at
        self.end_flag, self.empty_end = end_flag, empty_end
        self.requests: list[dict] = []

    async def open(self):
        return None

    async def request(self, req):
        self.requests.append(req.payload)
        if self.fail_at is not None and len(self.requests) == self.fail_at:
            return Response(status=500, error=Failure("status", "HTTP 500: disk quota exceeded on node 4", 500))
        a = req.payload.get("args", {})
        size = int(a.get("size") or 100)
        if self.max_rows is not None and size > self.max_rows:
            return Response(error=Failure("error", f"result of {size} rows is too large for the gateway"))
        after = a.get("after")
        rows = [r for r in self.rows if after is None or r["ts"] > after]
        start = int(a.get("token") or 0)
        take = max(1, size // 2) if self.short else size
        chunk = rows[start:start + take]
        nxt = start + len(chunk)
        body = {"items": chunk, "next": str(nxt) if nxt < len(rows) else None}
        if self.end_flag:
            body = {"items": chunk, "next": str(nxt), "end": nxt >= len(rows)}
        return Response(body=json.dumps(body).encode())

    async def close(self):
        return None


def source(**over):
    cfg = {"transport": "t", "cursor": {"model": "incremental"},
           "request": {"read": {"op": "read", "args": {"after": "{cursor.position}", "size": "{page.size}",
                                                       "token": "{page.token}"}}},
           "paging": {"entries": "$.items", "next": "$.next", "order_key": "$.ts", "page_size": 4, "min_page_size": 1},
           "contract": {"entry": {"required": {"id": "str", "ts": "timestamp"}}}}
    for k, v in over.items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) else v
    return Source.model_validate(cfg)


def read(cfg, server, cursor=None, unit=None):
    async def go():
        src = DeclarativeSource("feed", cfg, server, secrets=Secrets(),
                                limits=Limits(asyncio.Semaphore(4), asyncio.Semaphore(4)), env={})
        r = src.read(None, unit or Unit(ALL_UNITS),
                     cursor or Cursor(cfg.cursor.effective, None, None, None, INSTANT))
        pages = [p async for p in r]
        return r, pages
    with collecting() as col:
        r, pages = asyncio.run(go())
        return r, pages, col.drain()


def ids(pages):
    return [e.view["id"] for p in pages for e in p.entries if e.view]


def test_fewer_rows_than_requested_is_not_the_end_unless_the_contract_says_so():
    server = Server(short=True)
    r, pages, _ = read(source(), server)
    assert r.outcome.status == "read" and ids(pages) == [x["id"] for x in ROWS]
    assert all(len(p.entries) < p.page_size for p in pages)  # every page was short ...
    assert len(server.requests) == len(pages) > 1  # ... and the next page was still requested
    # declared: a short page ends the unit, so coverage ends where the reading stopped
    r2, pages2, _ = read(source(paging={"end_when": ["short_page"]}), Server(short=True))
    assert len(pages2) == 1 and r2.outcome.status == "read"


def test_incremental_proves_coverage_and_proposes_the_last_order_key():
    r, pages, _ = read(source(), Server())
    pos, cov, complete = r.proposal
    assert complete and pos == ROWS[-1]["ts"] and cov == INSTANT
    # the next read starts after the proposed position and reads nothing new
    r2, pages2, _ = read(source(), Server(), Cursor("incremental", pos, cov, cov, INSTANT))
    assert r2.outcome.status == "empty" and ids(pages2) == []
    assert r2.proposal == (pos, INSTANT, True)  # coverage proven to the interval end; the position holds


def test_end_flag_signal_with_a_token_position():
    cfg = source(cursor={"model": "incremental", "position": "token"},
                 paging={"end": "$.end", "end_when": ["flag"]})
    r, pages, _ = read(cfg, Server(end_flag=True))
    assert r.outcome.status == "read" and r.proposal[0] == "10" and r.proposal[2] is True


def test_no_continuation_and_no_end_signal_is_partial_not_complete():
    cfg = source(paging={"end": "$.end", "end_when": ["flag"], "next": "$.nothing"})
    r, pages, shapes = read(cfg, Server(end_flag=True))
    assert r.outcome.status == "partial" and r.proposal[2] is False
    assert [s.detail for s in shapes] == ["feed page without continuation or end signal"]


def test_snapshot_complete_only_when_paging_reached_the_end():
    cfg = source(cursor={"model": "snapshot"})
    r, _, _ = read(cfg, Server(), Cursor("snapshot", None, None, None, INSTANT))
    assert r.outcome.status == "read" and r.proposal == (INSTANT, INSTANT, True)
    r, _, _ = read(source(cursor={"model": "snapshot"}, budget={"max_pages": 1}), Server(),
                   Cursor("snapshot", None, None, None, INSTANT))
    assert r.outcome.status == "partial" and r.proposal[0] is None  # nothing proven: the cursor holds


def test_step_down_on_truncation_retries_the_same_position():
    server = Server(max_rows=1)
    cfg = source(paging={"page_size": 4, "min_page_size": 1, "too_large": ["too large"]})
    r, pages, _ = read(cfg, server)
    assert r.outcome.status == "read" and r.outcome.stepdowns == 2 and r.outcome.page_size == 1
    assert ids(pages) == [x["id"] for x in ROWS]
    assert [q["args"]["size"] for q in server.requests[:3]] == [4, 2, 1]
    assert "token" not in server.requests[0]["args"] and "token" not in server.requests[2]["args"]


def test_truncated_at_min_page_size_is_partial_with_its_retry_position():
    cfg = source(paging={"page_size": 4, "min_page_size": 2, "too_large": ["too large"]})
    r, pages, _ = read(cfg, Server(max_rows=1))
    assert r.outcome.status == "partial" and r.outcome.cause["code"] == "source.truncated"
    assert r.outcome.position is None and r.proposal[0] is None and pages == []


def test_size_cap_and_contract_marker_are_truncation_signals():
    class Capped(Server):
        async def request(self, req):
            resp = await super().request(req)
            if int(req.payload["args"]["size"]) > 2:
                resp.truncated = True
            return resp
    r, _, _ = read(source(), Capped())
    assert r.outcome.status == "read" and r.outcome.stepdowns == 1


def test_budget_ends_as_partial_never_failed_and_the_proposal_ends_at_the_gap():
    r, pages, _ = read(source(budget={"max_pages": 2}), Server())
    assert r.outcome.status == "partial" and r.outcome.cause["code"] == "source.budget_exhausted"
    assert r.proposal[0] == ROWS[7]["ts"] and r.proposal[2] is False  # two pages of four proven


def test_failure_keeps_the_raw_message_and_the_proven_prefix():
    r, pages, _ = read(source(), Server(fail_at=2))
    assert r.outcome.status == "failed"
    assert r.outcome.cause["message"] == "HTTP 500: disk quota exceeded on node 4" and r.outcome.cause["status"] == 500
    assert r.proposal[0] == ROWS[3]["ts"] and r.proposal[2] is False


def test_past_interval_ends_an_incremental_read():
    cfg = source(paging={"end_when": ["token_absent", "past_interval"]})
    r, pages, _ = read(cfg, Server(), Cursor("incremental", None, None, None, ROWS[2]["ts"]))
    assert len(pages) == 1 and r.outcome.status == "read"


def test_cadence_skips_and_never_reports_a_gap():
    cfg = source(cursor={"model": "cadence", "inner": "incremental", "weekdays": ["mon"]}).cursor
    assert cadence_skip(cfg, date(2026, 10, 15), None).startswith("cadence")  # a Thursday
    assert cadence_skip(cfg, date(2026, 10, 19), None) is None
    every = source(cursor={"model": "cadence", "every_days": 7}).cursor
    assert cadence_skip(every, date(2026, 10, 15), "2026-10-12T00:00:00+00:00") is not None
    assert cadence_skip(every, date(2026, 10, 20), "2026-10-12T00:00:00+00:00") is None


def test_positions_compare_by_number_then_instant_else_incomparable():
    assert compare("9", "10") == -1 and compare("2026-10-15T08:00:00Z", "2026-10-15T03:00:00-05:00") == 0
    assert compare("tokA", "tokB") is None


def test_proposals_move_forward_and_close_applies_forward_only(tmp_path):
    db = open_db(tmp_path / "d.db")
    db.row_factory = sqlite3.Row
    with db:
        db.execute("INSERT INTO cursors (source, unit, position, coverage_end, updated_run) VALUES "
                   "('feed','a','2026-10-15T05:00:00+00:00',NULL,'old'), ('feed','gap','7',NULL,'old')")
        assert write_proposal(db, "r1", "feed", "a", "2026-10-15T09:00:00+00:00", INSTANT, generation=0,
                              model="units", complete=True)
        assert not write_proposal(db, "r1", "feed", "a", "2026-10-15T08:00:00+00:00", INSTANT, generation=1,
                                  model="units", complete=True)  # a later generation never moves it back
        write_proposal(db, "r1", "feed", "b", "4", None, generation=0, model="units", complete=False)
        write_proposal(db, "r1", "feed", "gap", "3", None, generation=0, model="units", complete=False)
        moved = apply_proposals(db, "r1")
    rows = {r["unit"]: r["position"] for r in db.execute("SELECT unit, position FROM cursors")}
    assert rows == {"a": "2026-10-15T09:00:00+00:00", "b": "4", "gap": "7"}
    assert moved["regressions"] == [{"source": "feed", "unit": "gap", "proposed": "3", "durable": "7"}]
    assert db.execute("SELECT COUNT(*) FROM ledgers WHERE ledger='cursor_regressions'").fetchone()[0] == 1
    db.close()


def test_id_scan_reads_unprocessed_ids_each_individually(tmp_path):
    from docket.sources.collect import Starts, _read_one
    cfg = Source.model_validate({
        "transport": "t", "cursor": {"model": "id-scan"},
        "request": {"ids": {"op": "ids"}, "get": {"op": "get", "args": {"id": "{id}"}}},
        "discover": {"entries": "$.ids", "id": "$"},
        "paging": {"entries": "$.items", "next": None, "end_when": ["token_absent"]}})

    class Ids(Server):
        async def request(self, req):
            self.requests.append(req.payload)
            if req.payload["op"] == "ids":
                return Response(body=b'{"ids": ["c", "a", "b"]}')
            i = req.payload["args"]["id"]
            if i == "b":
                return Response(error=Failure("status", "HTTP 404: b is gone", 404))
            return Response(body=json.dumps({"items": [{"id": i}]}).encode())

    async def go():
        src = DeclarativeSource("ids", cfg, Ids(), secrets=Secrets(),
                                limits=Limits(asyncio.Semaphore(4), asyncio.Semaphore(4)), env={})
        return await _read_one(src, "ids", cfg, starts=Starts(processed={"ids": {"c"}}), instant=INSTANT,
                               run_date=date(2026, 10, 15), entry_limit=None, only_unit=None)
    res = asyncio.run(go())
    got = {u.unit.id: (u.outcome.status, u.proposal[2]) for u in res.units}
    assert got == {"a": ("read", True), "b": ("failed", False)} and res.outcome.status == "partial"


def test_a_plan_that_ignores_the_continuation_cannot_loop():
    cfg = Source.model_validate({"transport": "t", "cursor": {"model": "incremental"},
                                 "request": {"read": {"op": "read", "args": {"size": "{page.size}"}}},
                                 "paging": {"entries": "$.items", "next": "$.next", "page_size": 4}})
    server = Server()
    r, pages, shapes = read(cfg, server)
    assert r.outcome.status == "partial" and len(server.requests) == 1
    assert [s.area for s in shapes] == ["source.paging"]
