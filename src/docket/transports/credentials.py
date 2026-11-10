"""Credentials that can expire while a transport is open (TD §6.3, §6.8).

``Credentials`` holds the references behind a transport's credential headers and reads
them again whenever a header is needed, so a value another program renewed is picked up
without a restart. When a server answers 401 it runs each reference's ``refresh`` command
once, without a terminal, and the request is repeated with what the references hold then.
A rejection that survives is kept in ``rejected`` so the transport can say that the cause
is authentication and why the refresh did not help. No message carries a value.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from docket.transports.base import Secrets, child_env, kill_group, raw_message, resolve_credential

_OUTPUT_TAIL = 300


class Credentials:
    def __init__(self, refs: Mapping[str, tuple[str, Any]], *, env: Mapping[str, str], root: Path,
                 secrets: Secrets) -> None:
        self.refs = dict(refs)            # header name -> (value prefix, credential reference)
        self.env = env
        self.root = root
        self.secrets = secrets
        self.rejected: tuple[int, str] | None = None   # (HTTP status, why it stands)
        self._lock = asyncio.Lock()

    def __bool__(self) -> bool:
        return bool(self.refs)

    def headers(self) -> tuple[dict[str, str], str | None]:
        out: dict[str, str] = {}
        for name, (prefix, ref) in self.refs.items():
            val, problem = resolve_credential(ref, self.env, self.root, self.secrets)
            if problem:
                return out, problem
            out[name] = prefix + (val or "")
        return out, None

    def _commands(self) -> list[tuple[list[str], float]]:
        seen: list[tuple[list[str], float]] = []
        for _, ref in self.refs.values():
            cmd = getattr(ref, "refresh", None)
            if cmd and all(cmd != c for c, _ in seen):
                seen.append((list(cmd), float(getattr(ref, "refresh_timeout_s", 30.0) or 30.0)))
        return seen

    async def renew(self, sent: Mapping[str, str]) -> tuple[bool, str]:
        """After a 401 on a request that carried ``sent``. Returns (repeat the request, note)."""
        async with self._lock:
            now, problem = self.headers()
            if problem is None and now != dict(sent):
                return True, "the credential changed after the request was sent"  # renewed meanwhile
            commands = self._commands()
            if not commands:
                return False, problem or "no refresh command is configured for the credential"
            for cmd, timeout in commands:
                failed = await self._run(cmd, timeout)
                if failed:
                    return False, failed
            _, problem = self.headers()
            if problem:
                return False, f"after the refresh command ran, {problem}"
            return True, "the credential was still rejected after the refresh command ran"

    async def _run(self, cmd: list[str], timeout: float) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=child_env(self.env, {}), cwd=str(self.root),
                start_new_session=True)
        except OSError as e:
            return self.secrets.mask(f"refresh command could not start: {raw_message(e)}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            kill_group(proc)
            await proc.wait()
            return f"refresh command timed out after {timeout:g}s (treated as interactive)"
        if proc.returncode != 0:
            text = (err or out or b"").decode("utf-8", "replace").strip()[-_OUTPUT_TAIL:]
            return self.secrets.mask(f"refresh command exited with status {proc.returncode}" + (f": {text}" if text else ""))
        return None

    def rejection_message(self) -> str | None:
        if self.rejected is None:
            return None
        status, why = self.rejected
        return f"authentication rejected (HTTP {status}): {why}"


def auth_for(base: type, creds: Credentials) -> Any:
    """An auth object for an HTTP client whose ``Auth`` base class is ``base``. Two HTTP
    libraries share the auth-flow protocol and not the class, so the class is built here."""

    class _RefreshingAuth(base):  # type: ignore[misc, valid-type]
        def _apply(self, request: Any) -> dict[str, str]:
            headers, _ = creds.headers()
            for name, value in headers.items():
                request.headers[name] = value
            return headers

        async def async_auth_flow(self, request: Any) -> Any:
            sent = self._apply(request)
            response = yield request
            if response.status_code != 401:
                creds.rejected = None
                return
            again, note = await creds.renew(sent)
            if again:
                self._apply(request)
                response = yield request
                if response.status_code != 401:
                    creds.rejected = None
                    return
                if not creds._commands():
                    note = "no refresh command is configured for the credential"
            creds.rejected = (401, note)

        def sync_auth_flow(self, request: Any) -> Any:
            raise RuntimeError("credentials are renewed on the asynchronous path only")  # raise-ok: programming error, every transport is async

    return _RefreshingAuth()
