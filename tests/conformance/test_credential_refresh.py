"""Short-lived credentials (TD §6.3, §6.8): a credential reference may name a refresh
command. When a server answers 401 the transport runs the command once, reads the
credential again and repeats the request. A rejection that survives names authentication.

Every system is a local stub. The "identity provider" is a file holding the token the
server accepts now; the refresh command copies it into the client's store.
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

import pytest

from docket.config import check
from docket.config.source_models import TransportConfig
from docket.transports.base import Request, Secrets
from docket.transports.registry import build_transport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MCP_STUB = str(FIXTURES / "mcp_stub_server.py")
OLD = "tok-Expired-1a2b3c4d5e"   # invented credentials
NEW = "tok-Current-6f7a8b9c0d"

REFRESH = """\
import json, sys, time
issued, store, log = sys.argv[1:4]
time.sleep(0.05)
tok = open(issued).read().strip()
json.dump({"gw": {"tokens": {"access_token": tok}}}, open(store, "w"))
open(log, "a").write("ran\\n")
"""


class World:
    """The server's current token, the client's JSON store, and a refresh command joining them."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.issued = tmp / "issued"
        self.store = tmp / "store.json"
        self.log = tmp / "refresh.log"
        self.script = tmp / "refresh.py"
        self.script.write_text(REFRESH, encoding="utf-8")
        self.issued.write_text(NEW, encoding="utf-8")
        self.put(OLD)

    def put(self, tok: str) -> None:
        self.store.write_text(json.dumps({"gw": {"tokens": {"access_token": tok}}}), encoding="utf-8")

    @property
    def runs(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def bearer(self, refresh: list[str] | None | bool = True, **kw) -> dict:
        ref: dict = {"file": str(self.store), "json_path": ["gw", "tokens", "access_token"], **kw}
        if refresh is True:
            ref["refresh"] = [sys.executable, str(self.script), str(self.issued), str(self.store), str(self.log)]
        elif refresh:
            ref["refresh"] = refresh
        return ref


def make(cfg: dict, tmp_path: Path):
    secrets = Secrets()
    t, problem = build_transport("t", TransportConfig.model_validate(cfg), root=tmp_path,
                                 env={"PATH": "/usr/bin:/bin"}, log_dir=tmp_path / "logs", secrets=secrets)
    assert problem is None, problem
    return t, secrets


# --- plain HTTP ---------------------------------------------------------------------------


@pytest.fixture
def http_world(tmp_path):
    world = World(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ok = self.headers.get("Authorization") == "Bearer " + world.issued.read_text().strip()
            body = json.dumps({"items": [{"id": "a"}]} if ok else {"error": "invalid_token"}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    world.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield world
    srv.shutdown()


GET = Request({"method": "GET", "url": "/items"})


def test_an_expired_bearer_is_refreshed_once_and_the_request_repeated(http_world, tmp_path):
    async def go():
        t, secrets = make({"kind": "http_json", "url": http_world.url, "bearer": http_world.bearer()}, tmp_path)
        try:
            rs = await asyncio.gather(*[t.request(GET) for _ in range(6)])  # six requests meet the 401 together
            again = await t.request(GET)
        finally:
            await t.close()
        assert [r.error for r in rs] == [None] * 6 and again.error is None
        assert json.loads(rs[0].body) == {"items": [{"id": "a"}]}
        assert http_world.runs == 1  # one refresh, not one per rejected request
        assert secrets.mask(f"{OLD} {NEW}") == "[redacted] [redacted]"  # the new value is masked too
    asyncio.run(go())


def test_a_token_that_expires_mid_session_is_refreshed(http_world, tmp_path):
    async def go():
        http_world.put(NEW)
        t, _ = make({"kind": "http_json", "url": http_world.url, "bearer": http_world.bearer()}, tmp_path)
        try:
            assert (await t.request(GET)).error is None and http_world.runs == 0
            http_world.issued.write_text("tok-Rotated-2b3c4d5e6f", encoding="utf-8")  # the server moves on
            assert (await t.request(GET)).error is None and http_world.runs == 1
        finally:
            await t.close()
    asyncio.run(go())


def test_without_a_refresh_command_a_401_is_reported_as_authentication(http_world, tmp_path):
    async def go():
        t, _ = make({"kind": "http_json", "url": http_world.url, "bearer": http_world.bearer(refresh=False)}, tmp_path)
        try:
            r = await t.request(GET)
        finally:
            await t.close()
        assert r.error.status == 401 and r.error.kind == "unavailable"
        assert "authentication rejected (HTTP 401)" in r.error.message and "no refresh command" in r.error.message
        assert OLD not in r.error.message and http_world.runs == 0
    asyncio.run(go())


@pytest.mark.parametrize("command,expect", [
    ([sys.executable, "-c", "import sys; print('login needed: visit the sign-in page'); sys.exit(3)"],
     "refresh command exited with status 3: login needed: visit the sign-in page"),
    ([sys.executable, "-c", "import time; time.sleep(30)"], "refresh command timed out after 0.3s (treated as interactive)"),
    (["docket-no-such-refresher"], "refresh command could not start"),
    ([sys.executable, "-c", "pass"], "the credential was still rejected after the refresh command ran"),
])
def test_a_refresh_that_does_not_help_says_why(http_world, tmp_path, command, expect):
    async def go():
        bearer = http_world.bearer(refresh=command, refresh_timeout_s=0.3)
        t, _ = make({"kind": "http_json", "url": http_world.url, "bearer": bearer}, tmp_path)
        started = time.monotonic()
        try:
            r = await t.request(GET)
        finally:
            await t.close()
        assert r.error.kind == "unavailable" and r.error.status == 401
        assert "authentication rejected (HTTP 401)" in r.error.message and expect in r.error.message
        assert OLD not in r.error.message and time.monotonic() - started < 5
    asyncio.run(go())


def test_the_refresh_command_runs_without_a_terminal_and_with_a_scrubbed_environment(http_world, tmp_path, monkeypatch):
    monkeypatch.setenv("UNDECLARED_SECRET", "must-not-reach-the-child")
    seen = tmp_path / "seen.json"
    code = ("import json, os, sys; json.dump({'env': dict(os.environ), 'tty': sys.stdin.isatty(), "
            f"'stdin': sys.stdin.read()}}, open({str(seen)!r}, 'w'))")

    async def go():
        t, _ = make({"kind": "http_json", "url": http_world.url,
                     "bearer": http_world.bearer(refresh=[sys.executable, "-c", code])}, tmp_path)
        try:
            await t.request(GET)
        finally:
            await t.close()
    asyncio.run(go())
    got = json.loads(seen.read_text())
    assert got["tty"] is False and got["stdin"] == ""
    assert got["env"]["DOCKET_NONINTERACTIVE"] == "1" and "UNDECLARED_SECRET" not in got["env"]


# --- MCP over streamable HTTP ---------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def mcp_world(tmp_path):
    world = World(tmp_path)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, MCP_STUB, "--http", str(port), "--bearer-file", str(world.issued)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={"PATH": "/usr/bin:/bin"})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    world.url = f"http://127.0.0.1:{port}/mcp"
    yield world
    proc.kill()
    proc.wait(5)


CALL = Request({"tool": "list_items", "args": {"page_size": 2}})


def test_mcp_opens_with_an_expired_bearer_after_one_refresh_and_survives_a_rotation(mcp_world, tmp_path):
    async def go():
        t, _ = make({"kind": "mcp_http", "url": mcp_world.url, "bearer": mcp_world.bearer(), "timeout_s": 5}, tmp_path)
        try:
            assert await t.open() is None and mcp_world.runs == 1
            assert len(json.loads((await t.request(CALL)).body)["items"]) == 2
            mcp_world.issued.write_text("tok-Rotated-2b3c4d5e6f", encoding="utf-8")
            r = await t.request(CALL)
            assert r.error is None and len(json.loads(r.body)["items"]) == 2 and mcp_world.runs == 2
        finally:
            await t.close()
    asyncio.run(go())


def test_mcp_names_authentication_when_the_handshake_is_rejected(mcp_world, tmp_path):
    async def go():
        t, _ = make({"kind": "mcp_http", "url": mcp_world.url, "bearer": mcp_world.bearer(refresh=False),
                     "startup_timeout_s": 5}, tmp_path)
        try:
            f = await t.open()
        finally:
            await t.close()
        assert f is not None and f.kind == "unavailable" and f.status == 401
        assert "authentication rejected (HTTP 401)" in f.message and OLD not in f.message
    asyncio.run(go())


# --- configuration --------------------------------------------------------------------------

BASE = """\
version: 1
graphs:
  daily:
    touch: { run: ["true"] }
profile:
  timezone: UTC
  me: { name: Dana Reyes }
transports:
  gw:
    kind: mcp_http
    url: "http://127.0.0.1:9/mcp"
"""


def issues_of(tmp_path, bearer: str):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docket.yaml").write_text(BASE + f"    bearer: {bearer}\n", encoding="utf-8")
    ws, issues, _ = check(root)
    return ws, issues


def test_configuration_accepts_a_refresh_command(tmp_path):
    ws, issues = issues_of(tmp_path, "{ file: ~/.config/agent/auth.json, json_path: [gw, token], "
                                     "refresh: [agent, servers, list], refresh_timeout_s: 45 }")
    assert issues == []
    ref = ws.config.transports["gw"].bearer
    assert ref.refresh == ["agent", "servers", "list"] and ref.refresh_timeout_s == 45


def test_configuration_refuses_an_empty_refresh_command(tmp_path):
    _, issues = issues_of(tmp_path, "{ env: GATEWAY_TOKEN, refresh: [] }")
    assert [(i.code, i.path) for i in issues] == [("config.bad_credential", "transports.gw.bearer")]
