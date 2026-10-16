"""MCP transports over the official ``mcp`` SDK (TD §6.3).

``McpStdioTransport`` spawns the server once per collect and reuses the session
for every request. Request payload: ``{"tool": "...", "args": {...}}``. The
response body is the tool result's text content (or its structured content as
JSON when there is no text). A result flagged ``isError`` is a failure whose
message is the server's own text.

The SDK's client is an async context manager bound to the task that entered
it, while sources call the transport from many tasks. A runner task owns the
context for the transport's lifetime; requests go straight to the session.
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping

from docket.transports.base import Failure, Request, Response, Secrets, open_log, raw_message


def result_body(result: Any) -> bytes:
    texts = [c.text for c in (getattr(result, "content", None) or []) if getattr(c, "type", None) == "text"]
    if texts:
        return "".join(texts).encode("utf-8")
    sc = getattr(result, "structured_content", None)
    if sc is None:
        sc = getattr(result, "structuredContent", None)
    return json.dumps(sc, sort_keys=True).encode("utf-8") if sc is not None else b""


class McpTransport(ABC):
    kind = "mcp"

    def __init__(self, tid: str, *, timeout_s: float, startup_timeout_s: float, max_response_bytes: int | None,
                 secrets: Secrets) -> None:
        self.id = tid
        self.timeout_s = timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.max_response_bytes = max_response_bytes
        self.secrets = secrets
        self.info: dict[str, Any] = {}
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._failure: Failure | None = None
        self._lock = asyncio.Lock()
        self._opened = False

    @abstractmethod
    def _connect(self) -> Any:
        """An un-entered ``mcp.Client`` for this transport."""

    def _cleanup(self) -> None:
        return None

    async def _runner(self) -> None:
        try:
            async with self._connect() as client:
                self._client = client
                info = client.server_info
                self.info = {"name": getattr(info, "name", None), "protocol": client.protocol_version}
                self._ready.set()
                await self._stop.wait()
        except BaseException as e:  # the SDK raises exception groups from its task group
            if self._failure is None and not self._ready.is_set():
                self._failure = Failure("unavailable", self.secrets.mask(raw_message(e)))
            self._client = None
        finally:
            self._ready.set()
            self._cleanup()

    async def open(self) -> Failure | None:
        async with self._lock:
            if self._opened:
                return self._failure
            self._opened = True
            self._task = asyncio.create_task(self._runner())
            try:
                await asyncio.wait_for(self._ready.wait(), self.startup_timeout_s)
            except asyncio.TimeoutError:
                self._failure = Failure("timeout", f"MCP handshake timed out after {self.startup_timeout_s:g}s")
                self._stop.set()
            if self._client is None and self._failure is None:
                self._failure = Failure("unavailable", "MCP session closed during the handshake")
            return self._failure

    async def call(self, tool: str, args: dict[str, Any]) -> Response:
        f = await self.open()
        if f is not None:
            return Response(error=f)
        if self._client is None:
            return Response(error=Failure("unavailable", "MCP session is closed"))
        try:
            result = await asyncio.wait_for(self._client.call_tool(tool, args), self.timeout_s)
        except asyncio.TimeoutError:
            return Response(error=Failure("timeout", f"{tool} timed out after {self.timeout_s:g}s", retryable=True))
        except Exception as e:
            return Response(error=Failure("error", self.secrets.mask(raw_message(e))))
        body = result_body(result)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            return Response(error=Failure("error", self.secrets.mask(body.decode("utf-8", "replace"))))
        truncated = self.max_response_bytes is not None and len(body) > self.max_response_bytes
        return Response(body=body, truncated=truncated)

    async def request(self, req: Request) -> Response:
        tool = req.payload.get("tool")
        if not isinstance(tool, str):
            return Response(error=Failure("protocol", "MCP request has no tool"))
        return await self.call(tool, dict(req.payload.get("args") or {}))

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None


class McpStdioTransport(McpTransport):
    kind = "mcp_stdio"

    def __init__(self, tid: str, command: list[str], *, env: Mapping[str, str], cwd: Path | None,
                 log_dir: Path | None, **kw: Any) -> None:
        super().__init__(tid, **kw)
        self.command = list(command)
        self.env = dict(env)
        self.cwd = cwd
        self.log_dir = log_dir
        self._log: Any = None

    def _connect(self) -> Any:
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self._log = open_log(self.log_dir, self.id)
        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env=self.env,
                                       cwd=str(self.cwd) if self.cwd else None)
        return Client(stdio_client(params, errlog=self._log), mode="legacy")

    def _cleanup(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None
