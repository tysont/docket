"""The exec-protocol client (TD §17.2).

A long-lived child process reads JSON-lines requests on stdin and writes
JSON-lines responses on stdout; its stderr goes to a log file. The child runs
in its own session (no controlling TTY), with a scrubbed environment that
passes only ``PATH``, ``HOME``, ``LANG``, ``TMPDIR``, the variables the
destination declares in ``env``, and ``DOCKET_NONINTERACTIVE=1``. Every request
has a timeout; a timeout reports the command and the elapsed time.

``hello`` carries ``{"protocol": 1, "mode": "live" | "sandbox" | "scratch"}``
and, for ``sandbox``, ``base``: the target the twin is seeded from.
The result names the program, its protocol, its capabilities and whether it
supports ``sandbox`` (its own twin) and ``scratch`` (a disposable target for
``docket plugin check``) modes.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import IO, Any, Mapping

from docket.cli.errors import ExternalError

PROTOCOL = 1
PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


class ExecClient:
    def __init__(self, argv: list[str], *, cwd: Path, env: Mapping[str, str], declared_env: list[str] | None = None,
                 timeout_s: float = 20.0, log_path: Path | None = None, mode: str = "live", base: str | None = None,
                 name: str = "") -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.log_path = log_path
        self.mode = mode
        self.base = base
        self.name = name or (Path(argv[0]).name if argv else "program")
        keep = set(PASS_ENV) | set(declared_env or [])
        self.env = {k: v for k, v in env.items() if k in keep}
        self.env["DOCKET_NONINTERACTIVE"] = "1"
        self.proc: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None
        self._buf = b""
        self._id = 0
        self.hello: dict[str, Any] = {}

    # --- lifecycle ---------------------------------------------------------------
    def start(self) -> dict[str, Any]:
        if self.proc is not None:
            return self.hello
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_path.open("ab")
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=str(self.cwd), env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._log if self._log is not None else subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise ExternalError("dest.unavailable", f"{self.name}: cannot start {self.argv[0]}: {e.strerror}",
                                hint="docket doctor", details={"argv": self.argv, "error": str(e)}) from None
        hello_args: dict[str, Any] = {"protocol": PROTOCOL, "mode": self.mode}
        if self.base:
            hello_args["base"] = self.base
        res = self.call("hello", hello_args)
        if not isinstance(res, dict) or res.get("protocol") != PROTOCOL:
            self.close()
            raise ExternalError("dest.protocol", f"{self.name}: hello did not negotiate protocol {PROTOCOL}",
                                details={"hello": res})
        caps = res.get("capabilities")
        if not isinstance(caps, list):
            self.close()
            raise ExternalError("dest.protocol", f"{self.name}: hello result has no capabilities list",
                                details={"hello": res})
        if self.mode != "live" and not res.get(self.mode):
            self.close()
            raise ExternalError("dest.unavailable", f"{self.name} does not declare {self.mode} mode",
                                details={"hello": res, "mode": self.mode})
        self.hello = res
        return res

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                if p.stdin:
                    p.stdin.close()
                p.wait(timeout=2)
            except Exception:
                p.kill()
                p.wait()
            for s in (p.stdout,):
                if s:
                    s.close()
        if self._log is not None:
            self._log.close()
            self._log = None

    @property
    def capabilities(self) -> set[str]:
        self.start()
        return set(self.hello.get("capabilities") or [])

    # --- requests ----------------------------------------------------------------
    def _readline(self, deadline: float, started: float, op: str) -> bytes:
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        try:
            while b"\n" not in self._buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ExternalError("dest.unavailable",
                                        f"{self.name} {op}: no response after {time.monotonic() - started:.1f}s",
                                        details={"argv": self.argv, "op": op, "timeout_s": self.timeout_s})
                if not sel.select(timeout=left):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise ExternalError("dest.unavailable", f"{self.name} {op}: the program exited",
                                        details={"argv": self.argv, "op": op, "returncode": self.proc.poll(),
                                                 "log": str(self.log_path) if self.log_path else None})
                self._buf += chunk
        finally:
            sel.close()
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def call(self, op: str, args: dict[str, Any] | None = None) -> Any:
        if self.proc is None and op != "hello":
            self.start()
        assert self.proc is not None and self.proc.stdin is not None
        self._id += 1
        req = {"v": PROTOCOL, "id": self._id, "op": op, "args": args or {}}
        started = time.monotonic()
        try:
            self.proc.stdin.write((json.dumps(req, sort_keys=True) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            raise ExternalError("dest.unavailable", f"{self.name} {op}: cannot write to the program: {e}",
                                details={"argv": self.argv, "op": op}) from None
        raw = self._readline(started + self.timeout_s, started, op)
        try:
            resp = json.loads(raw)
        except ValueError:
            raise ExternalError("dest.protocol", f"{self.name} {op}: the response is not JSON",
                                details={"op": op, "raw": raw[:500].decode("utf-8", "replace")}) from None
        if not isinstance(resp, dict) or resp.get("id") != self._id:
            raise ExternalError("dest.protocol", f"{self.name} {op}: the response id does not match the request",
                                details={"op": op, "response": str(resp)[:500]})
        if not resp.get("ok"):
            err = resp.get("error") if isinstance(resp.get("error"), dict) else {"message": str(resp.get("error"))}
            raise ExternalError("dest.apply_failed", f"{self.name} {op}: {err.get('message') or err.get('code')}",
                                retryable=bool(err.get("retryable", False)), hint="docket apply",
                                details={"op": op, "error": err})
        return resp.get("result")
