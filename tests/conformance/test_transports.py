"""Transport conformance (TP §7.2): for each transport, handshake, paging to exhaustion,
step-down on truncation, timeout behavior, raw error preservation, and no credential in
any output. Every system is a local stub: a Python exec program, an in-process HTTP
server on 127.0.0.1, and an MCP server written with the SDK over stdio and HTTP.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from docket.config.source_models import Source, TransportConfig
from docket.sources.base import ALL_UNITS, Cursor, Unit
from docket.sources.declarative import DeclarativeSource, Limits
from docket.transports.base import Request, Secrets
from docket.transports.registry import build_transport
from docket.transports.replay import Recorder, ReplayTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STUB_SOURCE = str(FIXTURES / "bin" / "docket-stub-source")
MCP_STUB = str(FIXTURES / "mcp_stub_server.py")
TOKEN = "tok-Conformance-9f8e7d6c5b"  # an invented credential, placed in the test environment only
INSTANT = "2026-10-15T18:00:00+00:00"


def make(cfg: dict, tmp_path: Path, env: dict | None = None, secrets: Secrets | None = None):
    secrets = secrets or Secrets()
    t, problem = build_transport("t", TransportConfig.model_validate(cfg), root=tmp_path,
                                 env={"PATH": "/usr/bin:/bin", **(env or {})}, log_dir=tmp_path / "logs",
                                 secrets=secrets)
    assert problem is None, problem
    return t, secrets


async def drain(t, source: dict, unit: str = ALL_UNITS, model: str = "incremental"):
    cfg = Source.model_validate({"transport": "t", **source})
    src = DeclarativeSource("s", cfg, t, secrets=Secrets(), limits=Limits(asyncio.Semaphore(4), asyncio.Semaphore(4)),
                            env={})
    r = src.read(None, Unit(unit), Cursor(model, None, None, None, INSTANT))
    pages = [p async for p in r]
    return r.outcome, pages


def entry_ids(pages):
    return [e.view["id"] for p in pages for e in p.entries if e.view]


# --- exec_jsonl -------------------------------------------------------------------------

EXEC_SOURCE = {"cursor": {"model": "incremental", "position": "token"},
               "request": {"read": {"op": "read", "args": {"unit": "{unit.id}", "position": "{page.token}",
                                                           "page_size": "{page.size}"}}},
               "paging": {"entries": "$.result.items", "next": "$.result.next", "end": "$.result.end",
                          "end_when": ["flag"], "page_size": 3, "min_page_size": 1}}


def test_exec_jsonl_conformance(tmp_path):
    cfg = {"kind": "exec_jsonl", "command": [sys.executable, STUB_SOURCE], "timeout_s": 0.5,
           "env_from": {"STUB_TOKEN": {"env": "CONFORMANCE_FAKE_TOKEN"}}}

    async def go():
        t, secrets = make(cfg, tmp_path, {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        try:
            assert await t.open() is None
            assert t.info["name"] == "stub-source" and t.info["protocol"] == 1
            assert t.info["interactive"] is False  # DOCKET_NONINTERACTIVE=1 reached the child
            out, pages = await drain(t, EXEC_SOURCE, unit="u1")
            assert out.status == "read" and len(pages) == 3 and len(entry_ids(pages)) == 7
            boom = await t.request(Request({"op": "boom"}))
            assert boom.error.message == "permission: queue u9 is not readable by this account"
            slow = await t.request(Request({"op": "sleep", "args": {"seconds": 0.7}}))
            assert slow.error.kind == "timeout" and "timed out after 0.5s" in slow.error.message
            again = await t.request(Request({"op": "read", "args": {"unit": "u2", "page_size": 2}}))
            assert again.ok  # a timed-out request does not break the session
            leak = await t.request(Request({"op": "leak"}))
            assert TOKEN not in leak.error.message and "[redacted]" in leak.error.message
        finally:
            await t.close()
    asyncio.run(go())
    log = (tmp_path / "logs" / "t.stderr.log").read_text()
    assert "stub source started" in log and TOKEN not in log


def test_exec_jsonl_step_down_and_handshake_failures(tmp_path):
    async def go():
        t, _ = make({"kind": "exec_jsonl", "command": [sys.executable, STUB_SOURCE, "--max-page", "2"]}, tmp_path)
        try:
            out, pages = await drain(t, {**EXEC_SOURCE, "paging": {**EXEC_SOURCE["paging"], "page_size": 8}}, "u1")
            assert out.status == "read" and out.stepdowns == 2 and out.page_size == 2 and len(entry_ids(pages)) == 7
        finally:
            await t.close()
        t, _ = make({"kind": "exec_jsonl", "command": [sys.executable, STUB_SOURCE, "--protocol", "2"]}, tmp_path)
        f = await t.open()
        await t.close()
        assert f.kind == "protocol" and "protocol 2" in f.message
        t, _ = make({"kind": "exec_jsonl", "command": [sys.executable, STUB_SOURCE, "--exit-on-hello"]}, tmp_path)
        f = await t.open()
        await t.close()
        assert f.kind == "unavailable" and f.message == "child exited with status 9: fatal: config file missing"
        t, _ = make({"kind": "exec_jsonl", "command": ["docket-no-such-program"]}, tmp_path)
        f = await t.open()
        assert f.kind == "unavailable" and "docket-no-such-program" in f.message
    asyncio.run(go())
    assert "fatal: config file missing" in (tmp_path / "logs" / "t.stderr.log").read_text()


# --- http_json ---------------------------------------------------------------------------

ITEMS = [{"id": f"h-{i}", "updated_at": f"2026-10-15T0{i}:00:00Z", "title": f"item {i}"} for i in range(9)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output quiet
        return

    def _send(self, status, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/fail":
            return self._send(503, b"shard 7 offline; retry in 30s")
        if u.path == "/slow":
            time.sleep(1.0)
            return self._send(200, {"items": []})
        if u.path == "/echo-auth":
            return self._send(500, ("bad credentials: " + self.headers.get("Authorization", "")).encode())
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "missing bearer"})
        limit = int(q.get("limit", 100))
        if limit > 4:
            return self._send(413, b"response too large")
        start = int(q.get("cursor", 0))
        chunk = ITEMS[start:start + limit]
        nxt = start + len(chunk)
        return self._send(200, {"items": chunk, "next": str(nxt) if nxt < len(ITEMS) else None})


@pytest.fixture
def http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads, srv.block_on_close = True, False
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


HTTP_SOURCE = {"cursor": {"model": "incremental"},
               "request": {"read": {"method": "GET", "url": "/items",
                                    "params": {"limit": "{page.size}", "cursor": "{page.token}"}}},
               "paging": {"entries": "$.items", "next": "$.next", "page_size": 8, "min_page_size": 1,
                          "order_key": "$.updated_at"}}


def test_http_json_conformance(tmp_path, http_server):
    cfg = {"kind": "http_json", "url": http_server, "bearer": {"env": "CONFORMANCE_FAKE_TOKEN"}, "timeout_s": 0.3}
    rec = tmp_path / "rec.jsonl"

    async def go():
        t, secrets = make(cfg, tmp_path, {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        t = Recorder(t, rec, secrets)
        try:
            out, pages = await drain(t, HTTP_SOURCE)
            assert out.status == "read" and out.stepdowns == 1 and len(entry_ids(pages)) == 9 and len(pages) == 3
            fail = await t.request(Request({"method": "GET", "url": "/fail"}))
            assert fail.error.status == 503 and fail.error.message == "HTTP 503: shard 7 offline; retry in 30s"
            slow = await t.request(Request({"method": "GET", "url": "/slow"}))
            assert slow.error.kind == "timeout"
            leak = await t.request(Request({"method": "GET", "url": "/echo-auth"}))
            assert TOKEN not in leak.error.message and "[redacted]" in leak.error.message
        finally:
            await t.close()
        # a body over the size cap is a truncation signal
        capped, _ = make({**cfg, "max_response_bytes": 200}, tmp_path, {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        try:
            out, pages = await drain(capped, HTTP_SOURCE)
            assert out.status == "read" and out.stepdowns >= 2 and out.page_size <= 2 and len(entry_ids(pages)) == 9
        finally:
            await capped.close()
    asyncio.run(go())
    assert TOKEN not in rec.read_text()
    # a missing credential names the variable and never starts the transport
    t, problem = build_transport("t", TransportConfig.model_validate(cfg), root=tmp_path, env={}, log_dir=None,
                                 secrets=Secrets())
    assert t is None and problem.message == "credential environment variable CONFORMANCE_FAKE_TOKEN is not set"


# --- MCP over stdio, over HTTP, and codemode ---------------------------------------------

MCP_SOURCE = {"cursor": {"model": "incremental", "position": "token"},
              "request": {"read": {"tool": "list_items", "args": {"token": "{page.token}", "page_size": "{page.size}"}}},
              "paging": {"entries": "$.items", "next": "$.next", "page_size": 8, "min_page_size": 1,
                         "too_large": ["too large"]}}


async def mcp_checks(t):
    assert await t.open() is None
    assert t.info["name"] == "stub-mcp"
    out, pages = await drain(t, MCP_SOURCE)
    assert out.status == "read" and out.stepdowns == 1 and len(entry_ids(pages)) == 9 and len(pages) == 3
    boom = await t.request(Request({"tool": "boom"}))
    assert boom.error is not None and "shard 7 offline (code 503)" in boom.error.message
    leak = await t.request(Request({"tool": "leak"}))
    assert TOKEN not in leak.error.message and "[redacted]" in leak.error.message
    slow = await t.request(Request({"tool": "slow", "args": {"seconds": 2}}))
    assert slow.error.kind == "timeout"


def test_mcp_stdio_conformance(tmp_path):
    cfg = {"kind": "mcp_stdio", "command": [sys.executable, MCP_STUB], "timeout_s": 0.5,
           "env_from": {"STUB_TOKEN": {"env": "CONFORMANCE_FAKE_TOKEN"}}}

    async def go():
        t, _ = make(cfg, tmp_path, {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        try:
            await mcp_checks(t)
        finally:
            await t.close()
        bad, _ = make({"kind": "mcp_stdio", "command": ["docket-no-such-server"], "startup_timeout_s": 5}, tmp_path)
        f = await bad.open()
        await bad.close()
        assert f.kind == "unavailable" and f.message
    asyncio.run(go())


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mcp_http_url():
    port = _free_port()
    env = {"PATH": "/usr/bin:/bin", "STUB_TOKEN": TOKEN}
    proc = subprocess.Popen([sys.executable, MCP_STUB, "--http", str(port)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.kill()
    proc.wait(5)


def test_mcp_http_conformance(tmp_path, mcp_http_url):
    cfg = {"kind": "mcp_http", "url": mcp_http_url, "bearer": {"env": "CONFORMANCE_FAKE_TOKEN"}, "timeout_s": 0.5}

    async def go():
        t, secrets = make(cfg, tmp_path, {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        try:
            await mcp_checks(t)
        finally:
            await t.close()
        refused, _ = make({**cfg, "url": f"http://127.0.0.1:{_free_port()}/mcp", "startup_timeout_s": 5}, tmp_path,
                          {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        f = await refused.open()
        await refused.close()
        assert f is not None and f.kind in ("unavailable", "timeout") and TOKEN not in f.message
    asyncio.run(go())


def test_codemode_renders_the_script_and_pages(tmp_path, mcp_http_url):
    cfg = {"kind": "codemode", "url": mcp_http_url, "tool": "execute", "timeout_s": 2}
    source = {"cursor": {"model": "incremental", "position": "token"},
              "request": {"read": {"script": 'CALL list_items {"token": {{token}}, "page_size": {{size}}}',
                                   "args": {"token": "{page.token}", "size": "{page.size}"}}},
              "paging": {"entries": "$.items", "next": "$.next", "page_size": 8, "min_page_size": 2,
                         "too_large": ["too large"]}}

    async def go():
        t, _ = make(cfg, tmp_path)
        try:
            out, pages = await drain(t, source)
            assert out.status == "read" and out.stepdowns == 1 and len(entry_ids(pages)) == 9
            assert pages[1].request["args"] == {"token": "4", "size": 4}  # the record keeps the parameters
            bad = await t.request(Request({"script": "CALL list_items {{x}}", "args": {"x": '"}; drop'}}))
            assert bad.error is not None  # a parameter renders as a JSON literal and cannot break out
        finally:
            await t.close()
    asyncio.run(go())
    from docket.transports.codemode import render_script
    assert render_script("f({{a}}, {{b.c}})", {"a": 'x"y', "b": {"c": 3}}) == 'f("x\\"y", 3)'


# --- files and replay ----------------------------------------------------------------------


def test_files_transport_pages_by_line_offset(tmp_path):
    (tmp_path / "feed.jsonl").write_text("".join(json.dumps({"id": f"f{i}"}) + "\n" for i in range(5)) + "\nnot json\n")
    source = {"cursor": {"model": "incremental", "position": "token"},
              "request": {"read": {"path": "feed.jsonl", "args": {"offset": "{page.token}", "limit": "{page.size}"}}},
              "paging": {"entries": "$.entries", "next": "$.next", "end": "$.end", "end_when": ["flag"],
                         "page_size": 2, "min_page_size": 1}}

    async def go():
        t, _ = make({"kind": "files"}, tmp_path)
        out, pages = await drain(t, source)
        assert out.status == "read" and entry_ids(pages) == [f"f{i}" for i in range(5)] and out.failed_records == 1
        missing = await t.request(Request({"path": "nope.jsonl"}))
        assert missing.error.kind == "unavailable" and missing.error.message == "file not found: nope.jsonl"
    asyncio.run(go())


def test_recorder_and_replay_round_trip_with_redaction(tmp_path, http_server):
    rec = tmp_path / "rec.jsonl"
    cfg = {"kind": "http_json", "url": http_server, "bearer": {"env": "CONFORMANCE_FAKE_TOKEN"},
           "record": {"path": "rec.jsonl", "redact": ["$.items.title"]}}

    async def go():
        t, _ = make(cfg, tmp_path, {"CONFORMANCE_FAKE_TOKEN": TOKEN})
        try:
            live, live_pages = await drain(t, HTTP_SOURCE)
            await t.request(Request({"method": "GET", "url": "/fail", "headers": {"Authorization": "x-live"}}))
        finally:
            await t.close()
        replay = ReplayTransport("r", rec)
        played, pages = await drain(replay, HTTP_SOURCE)
        assert played.status == live.status and entry_ids(pages) == entry_ids(live_pages)
        assert played.stepdowns == live.stepdowns == 1  # the recorded 413 replays as a step-down
        miss = await replay.request(Request({"method": "GET", "url": "/never"}))
        assert miss.error.kind == "no_recording" and miss.error.data["digest"]
    asyncio.run(go())
    text = rec.read_text()
    assert TOKEN not in text and "x-live" not in text and '"title": "[redacted]"' in text
    assert all("request" in json.loads(ln) for ln in text.splitlines())
