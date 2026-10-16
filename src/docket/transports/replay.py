"""Recorded transports (TP §7.1).

``Recorder`` wraps any transport and appends each request/response pair to a
JSON-lines file. Credential-named request keys and configured response paths
are redacted, and every value read from a credential source is masked, before
anything is written. ``ReplayTransport`` serves the pairs back by request
digest, so a recorded session replays with no system and no credential.

A recording line:

    {"request": {...payload...},
     "response": {"body_json": {...}} | {"body": "text"},
                 "status": 200, "truncated": false,
                 "error": {"kind": "status", "message": "...", "status": 500} | null}

``body_json`` keeps hand-written fixtures readable; it is served as compact JSON.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docket.config.digest import digest
from docket.transports.base import Failure, Request, Response, Secrets, redact_mapping


def _redact_paths(value: Any, paths: list[str]) -> Any:
    for p in paths:
        parts = p[2:].split(".") if p.startswith("$.") else p.split(".")
        cur = value
        for part in parts[:-1]:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(cur, dict) and parts[-1] in cur:
            cur[parts[-1]] = "[redacted]"
        elif isinstance(cur, list):
            for el in cur:
                if isinstance(el, dict) and parts[-1] in el:
                    el[parts[-1]] = "[redacted]"
    return value


def encode_response(resp: Response, secrets: Secrets | None = None, redact: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": resp.status, "truncated": resp.truncated, "error": None}
    if resp.error is not None:
        e = resp.error
        msg = secrets.mask(e.message) if secrets else e.message
        out["error"] = {"kind": e.kind, "message": msg, "status": e.status, "retryable": e.retryable}
    text = resp.body.decode("utf-8", "replace")
    if secrets:
        text = secrets.mask(text)
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    if parsed is not None and isinstance(parsed, (dict, list)):
        out["body_json"] = _redact_paths(parsed, list(redact or []))
    else:
        out["body"] = text
    return out


def decode_response(row: dict[str, Any]) -> Response:
    if "body_json" in row:
        body = json.dumps(row["body_json"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        body = str(row.get("body") or "").encode("utf-8")
    err = row.get("error")
    failure = None
    if isinstance(err, dict):
        failure = Failure(err.get("kind") or "error", str(err.get("message") or ""), err.get("status"),
                          bool(err.get("retryable", False)))
    return Response(body=body if failure is None else b"", status=row.get("status"),
                    truncated=bool(row.get("truncated")), error=failure)


class ReplayTransport:
    kind = "replay"

    def __init__(self, tid: str, path: Path) -> None:
        self.id = tid
        self.path = path
        self.info: dict[str, Any] = {"name": "replay", "protocol": 1}
        self._pairs: dict[str, dict[str, Any]] | None = None
        self._problem: str | None = None

    async def open(self) -> Failure | None:
        if self._pairs is not None:
            return None
        if not self.path.is_file():
            self._problem = f"recordings file not found: {self.path.name}"
            return Failure("unavailable", self._problem)
        pairs: dict[str, dict[str, Any]] = {}
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                pairs[digest(row["request"])] = row["response"]
            except (ValueError, KeyError, TypeError):
                return Failure("protocol", f"{self.path.name}:{n}: not a recording line")
        self._pairs = pairs
        return None

    async def request(self, req: Request) -> Response:
        if self._pairs is None:
            f = await self.open()
            if f is not None:
                return Response(error=f)
        row = (self._pairs or {}).get(req.digest)
        if row is None:
            summary = json.dumps(req.payload, sort_keys=True)[:300]
            return Response(error=Failure("no_recording", f"no recording for request {req.digest[:12]}: {summary}",
                                          data={"digest": req.digest}))
        return decode_response(row)

    async def close(self) -> None:
        return None


class Recorder:
    """Wraps ``inner`` and appends each pair to ``path``, one line per distinct request digest."""

    def __init__(self, inner: Any, path: Path, secrets: Secrets, redact: list[str] | None = None) -> None:
        self.inner = inner
        self.id = inner.id
        self.kind = inner.kind
        self.path = path
        self.secrets = secrets
        self.redact = list(redact or [])
        self._seen: set[str] | None = None

    @property
    def info(self) -> dict[str, Any]:
        return self.inner.info

    async def open(self) -> Failure | None:
        return await self.inner.open()

    def _load_seen(self) -> set[str]:
        if self._seen is None:
            self._seen = set()
            if self.path.is_file():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        self._seen.add(digest(json.loads(line)["request"]))
                    except (ValueError, KeyError, TypeError):
                        continue
        return self._seen

    async def request(self, req: Request) -> Response:
        resp = await self.inner.request(req)
        seen = self._load_seen()
        if req.digest not in seen:
            seen.add(req.digest)
            payload = redact_mapping(json.loads(self.secrets.mask(json.dumps(req.payload, sort_keys=True))))
            row = {"request": payload, "response": encode_response(resp, self.secrets, self.redact)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        return resp

    async def close(self) -> None:
        await self.inner.close()
