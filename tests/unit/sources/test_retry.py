"""Transient failures (TD §6.6): a request that fails for a passing reason is repeated a
bounded number of times before the unit or the source is failed. A failure the transport
marks retryable, or whose message matches a ``retry.transient`` pattern, is transient. A
truncation is not: the pager steps down from it. Every repeat is counted in the outcome.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from docket.config.source_models import Source
from docket.sources.collect import Starts, _read_one
from docket.sources.declarative import DeclarativeSource, Limits
from docket.transports.base import Failure, Response, Secrets

pytestmark = pytest.mark.smoke
INSTANT = "2026-10-15T18:00:00+00:00"
ROWS = [{"id": f"r{i}", "ts": f"2026-10-15T0{i}:00:00+00:00"} for i in range(6)]


class Flaky:
    """Serves ROWS in pages; ``fail`` maps a request number (from 1) to the failure it meets."""
    kind, id, info = "scripted", "t", {}

    def __init__(self, fail: dict[int, Failure]):
        self.fail = fail
        self.requests: list[dict] = []

    async def open(self):
        return None

    async def close(self):
        return None

    async def request(self, req):
        self.requests.append(req.payload)
        f = self.fail.get(len(self.requests))
        if f is not None:
            return Response(error=f)
        a = req.payload.get("args", {})
        size = int(a.get("size") or 100)
        if size > 4:
            return Response(error=Failure("error", "response too large for the gateway"))
        start = int(a.get("token") or 0)
        chunk = ROWS[start:start + size]
        nxt = start + len(chunk)
        return Response(body=json.dumps({"items": chunk, "next": str(nxt) if nxt < len(ROWS) else None}).encode())


def source(**retry):
    return Source.model_validate({
        "transport": "t", "cursor": {"model": "incremental"},
        "request": {"read": {"op": "read", "args": {"size": "{page.size}", "token": "{page.token}"}}},
        "paging": {"entries": "$.items", "next": "$.next", "order_key": "$.ts", "page_size": 2, "min_page_size": 1,
                   "too_large": ["too large"]},
        "retry": {"wait_s": 0, **retry},
        "contract": {"entry": {"required": {"id": "str", "ts": "timestamp"}}}})


def read_once(cfg, server):
    async def go():
        src = DeclarativeSource("feed", cfg, server, secrets=Secrets(),
                                limits=Limits(asyncio.Semaphore(4), asyncio.Semaphore(4)), env={})
        return await _read_one(src, "feed", cfg, starts=Starts(), instant=INSTANT, run_date=date(2026, 10, 15),
                               entry_limit=None, only_unit=None)
    return asyncio.run(go())


def ids(res):
    return [e.view["id"] for u in res.units for p in u.pages for e in p.entries if e.view]


def test_a_retryable_failure_is_repeated_and_the_read_completes():
    server = Flaky({2: Failure("timeout", "read timed out after 30s", retryable=True)})
    res = read_once(source(), server)
    assert res.outcome.status == "read" and ids(res) == [r["id"] for r in ROWS]
    assert res.outcome.retries == 1 and len(server.requests) == 4  # three pages and one repeat
    assert server.requests[1] == server.requests[2]                # the same request, same position


def test_a_message_matching_a_transient_pattern_is_repeated():
    server = Flaky({1: Failure("error", "Error: The operation was aborted due to timeout"),
                    3: Failure("error", "SSE stream ended and reconnection attempts were exhausted")})
    res = read_once(source(transient=["aborted due to timeout", "stream ended"]), server)
    assert res.outcome.status == "read" and ids(res) == [r["id"] for r in ROWS] and res.outcome.retries == 2


def test_a_failure_that_is_not_transient_is_not_repeated():
    server = Flaky({1: Failure("error", "Invalid arguments for tool: expected string")})
    res = read_once(source(transient=["stream ended"]), server)
    assert res.outcome.status == "failed" and len(server.requests) == 1 and res.outcome.retries == 0
    assert "Invalid arguments" in res.outcome.cause["message"]


def test_repeats_are_bounded_and_the_last_failure_is_kept():
    always = {n: Failure("status", "HTTP 503: shard 7 offline", 503, retryable=True) for n in range(1, 10)}
    server = Flaky(always)
    res = read_once(source(attempts=2), server)
    assert res.outcome.status == "failed" and len(server.requests) == 3  # the request and two repeats
    assert res.outcome.cause["message"] == "HTTP 503: shard 7 offline" and res.outcome.retries == 2


def test_attempts_zero_turns_repeating_off():
    server = Flaky({1: Failure("timeout", "read timed out", retryable=True)})
    res = read_once(source(attempts=0), server)
    assert res.outcome.status == "failed" and len(server.requests) == 1


def test_a_truncation_is_stepped_down_from_never_repeated():
    cfg = source(transient=["too large"])  # even when a pattern would match it
    cfg.paging.page_size = 8
    server = Flaky({})
    res = read_once(cfg, server)
    assert res.outcome.status == "read" and res.outcome.retries == 0 and res.outcome.stepdowns == 1
    assert [q["args"]["size"] for q in server.requests[:2]] == [8, 4]


def test_an_authentication_rejection_is_not_repeated():
    server = Flaky({1: Failure("unavailable", "authentication rejected (HTTP 401): no refresh command", 401)})
    res = read_once(source(transient=["rejected"]), server)
    assert res.outcome.status == "unavailable" and len(server.requests) == 1
