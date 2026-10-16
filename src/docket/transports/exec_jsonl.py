"""The ``exec_jsonl`` transport: a long-lived child speaking the Docket protocol (TD §17.2).

One process per transport instance per collect, never per item. Requests and
responses are JSON lines; ``id`` pairs them, so several units can share one
child. ``open`` performs the ``hello`` handshake and refuses a protocol it does
not speak. Request payload: ``{"op": "...", "args": {...}}``. The response body
is the raw response line; the result is under ``result`` and an error under
``error`` with the child's own code and message.

The child runs with no controlling terminal (a new session), a scrubbed
environment with ``DOCKET_NONINTERACTIVE=1``, and stderr appended to the run's
log for this transport.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from docket.transports.base import Failure, Request, Response, Secrets, kill_group, open_log, raw_message

PROTOCOL = 1
_READ_LIMIT = 64 * 1024 * 1024


class ExecJsonlTransport:
    kind = "exec_jsonl"

    def __init__(self, tid: str, command: list[str], *, env: Mapping[str, str], cwd: Path | None,
                 timeout_s: float, startup_timeout_s: float, max_response_bytes: int | None,
                 log_dir: Path | None, secrets: Secrets) -> None:
        self.id = tid
        self.command = list(command)
        self.env = dict(env)
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.max_response_bytes = max_response_bytes
        self.log_dir = log_dir
        self.secrets = secrets
        self.info: dict[str, Any] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[bytes]] = {}
        self._next_id = 1
        self._dead: str | None = None
        self._log: Any = None
        self._lock = asyncio.Lock()
        self._opened = False
        self._open_failure: Failure | None = None

    async def open(self) -> Failure | None:
        async with self._lock:
            if self._opened:
                return self._open_failure
            self._opened = True
            self._open_failure = await self._start()
            return self._open_failure

    async def _start(self) -> Failure | None:
        self._log = open_log(self.log_dir, self.id)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self._log,
                env=self.env, cwd=str(self.cwd) if self.cwd else None, start_new_session=True,
                limit=_READ_LIMIT)
        except OSError as e:
            return Failure("unavailable", self.secrets.mask(f"cannot start {self.command[0]}: {raw_message(e)}"))
        self._reader = asyncio.create_task(self._read_loop())
        resp = await self._call("hello", {"protocol": PROTOCOL}, self.startup_timeout_s)
        if resp.error is not None:
            return resp.error
        try:
            msg = json.loads(resp.body)
            result = msg.get("result") or {}
        except (ValueError, AttributeError):
            return Failure("protocol", "hello response is not a JSON object")
        if result.get("protocol") != PROTOCOL:
            return Failure("protocol", f"child speaks protocol {result.get('protocol')!r}; Docket speaks {PROTOCOL}")
        self.info = result
        return None

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    rid = json.loads(line).get("id")
                except (ValueError, AttributeError):
                    continue
                fut = self._pending.pop(rid, None) if isinstance(rid, int) else None
                if fut is not None and not fut.done():
                    fut.set_result(line.rstrip(b"\n"))
        except (ValueError, asyncio.LimitOverrunError) as e:
            self._dead = f"response stream broken: {raw_message(e)}"
        finally:
            try:
                code = await asyncio.wait_for(self._proc.wait(), 2.0)
            except asyncio.TimeoutError:
                code = None
            last = self._stderr_tail()
            self._dead = self._dead or (f"child exited{'' if code is None else f' with status {code}'}"
                                        + (f": {last}" if last else ""))
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result(b"")
            self._pending.clear()

    def _stderr_tail(self) -> str:
        """The child's last stderr line, masked: its own account of why it stopped."""
        path = (self.log_dir / f"{self.id}.stderr.log") if self.log_dir is not None else None
        if path is None or not path.is_file():
            return ""
        try:
            if self._log is not None:
                self._log.flush()
            lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            return ""
        return self.secrets.mask(lines[-1][-500:]) if lines else ""

    async def _call(self, op: str, args: dict[str, Any], timeout: float) -> Response:
        if self._proc is None or self._proc.stdin is None:
            return Response(error=Failure("unavailable", "transport is not open"))
        if self._dead:
            return Response(error=Failure("unavailable", self._dead))
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"v": PROTOCOL, "id": rid, "op": op, "args": args}, sort_keys=True) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._pending.pop(rid, None)
            return Response(error=Failure("unavailable", f"child is gone: {raw_message(e)}"))
        try:
            raw = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return Response(error=Failure("timeout", f"{op} timed out after {timeout:g}s", retryable=True))
        if not raw:
            return Response(error=Failure("unavailable", self._dead or "child closed its output"))
        if self.max_response_bytes is not None and len(raw) > self.max_response_bytes:
            return Response(body=raw, truncated=True)
        try:
            msg = json.loads(raw)
        except ValueError:
            return Response(error=Failure("protocol", "response line is not JSON"))
        if msg.get("ok") is False:
            err = msg.get("error") or {}
            text = str(err.get("message") or "")
            code = err.get("code")
            kind = "too_large" if code in ("too_large", "truncated") else ("timeout" if code == "timeout" else "error")
            return Response(body=raw, error=Failure(kind, self.secrets.mask(f"{code}: {text}" if code else text),
                                                    retryable=bool(err.get("retryable", False)),
                                                    data={"code": code}))
        return Response(body=raw)

    async def request(self, req: Request) -> Response:
        op = req.payload.get("op")
        if not isinstance(op, str):
            return Response(error=Failure("protocol", "exec request has no op"))
        opened = await self.open()
        if opened is not None:
            return Response(error=opened)
        return await self._call(op, dict(req.payload.get("args") or {}), self.timeout_s)

    async def close(self) -> None:
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except asyncio.TimeoutError:
                kill_group(proc)
                await proc.wait()
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, 1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
        if self._log is not None:
            self._log.close()
            self._log = None
