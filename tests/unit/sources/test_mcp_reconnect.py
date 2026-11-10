"""An MCP session that dies mid-collect is reopened once and the call repeated (TD §6.3).

The SDK reports a dead stream as an exception from ``call_tool``. A tool result flagged as
an error is the server answering: the session is alive and is left alone.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from docket.transports.base import Request, Secrets
from docket.transports.mcp_stdio import McpTransport

pytestmark = pytest.mark.smoke


class Client:
    def __init__(self, world, number):
        self.world, self.number = world, number
        self.server_info = SimpleNamespace(name="stub")
        self.protocol_version = "1"

    async def call_tool(self, tool, args):
        self.world.calls.append((self.number, tool))
        if tool == "tool_error":
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="shard 7 offline")], is_error=True)
        if self.number in self.world.dead_sessions:
            raise RuntimeError("SSE stream ended and reconnection attempts were exhausted")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')], is_error=False)


class World:
    def __init__(self, dead_sessions=()):
        self.dead_sessions = set(dead_sessions)
        self.sessions = 0
        self.calls: list[tuple[int, str]] = []


class Fake(McpTransport):
    def __init__(self, world):
        super().__init__("t", timeout_s=2, startup_timeout_s=2, max_response_bytes=None, secrets=Secrets())
        self.world = world

    def _connect(self):
        world = self.world

        class Ctx:
            async def __aenter__(self):
                world.sessions += 1
                return Client(world, world.sessions)

            async def __aexit__(self, *exc):
                return None
        return Ctx()


CALL = Request({"tool": "list", "args": {}})


def run(world, *requests):
    async def go():
        t = Fake(world)
        try:
            return [await t.request(r) for r in requests]
        finally:
            await t.close()
    return asyncio.run(go())


def test_a_dead_session_is_reopened_once_and_the_call_repeated():
    world = World(dead_sessions={1})
    first, second = run(world, CALL, CALL)
    assert first.error is None and first.body == b'{"ok": true}' and second.error is None
    assert world.sessions == 2 and world.calls == [(1, "list"), (2, "list"), (2, "list")]


def test_when_the_new_session_fails_too_the_failure_is_returned_marked_retryable():
    world = World(dead_sessions={1, 2})
    (r,) = run(world, CALL)
    assert r.error.kind == "error" and "stream ended" in r.error.message and r.error.retryable
    assert world.sessions == 2  # one reopen per call, not a loop


def test_a_tool_error_leaves_the_session_alone():
    world = World()
    (r,) = run(world, Request({"tool": "tool_error", "args": {}}))
    assert r.error.message == "shard 7 offline" and world.sessions == 1


def test_concurrent_calls_on_a_dead_session_share_one_reopen():
    world = World(dead_sessions={1})

    async def go():
        t = Fake(world)
        try:
            return await asyncio.gather(*[t.request(CALL) for _ in range(5)])
        finally:
            await t.close()
    rs = asyncio.run(go())
    assert [r.error for r in rs] == [None] * 5 and world.sessions == 2
