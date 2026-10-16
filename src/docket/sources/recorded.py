"""Replay over a run's recorded records, for rehearsal (TD §14.3).

A rehearsal never reads a live system. While a ``RecordedReplay`` is active
(``replaying(...)``), collection swaps every transport for one that answers
each request from the run's own records: the record index under
``gen-<n>/records/`` holds each page's request payload and the blob of its
body, so a request with the same digest gets the same body back. Auth checks
are skipped (reported as such). A request the run never made is answered from
the configured transport only when that transport is local and read-only
(``replay`` recordings or ``files``); otherwise it fails with ``no_recording``
and the source's outcome says so, without contacting anything.
"""
from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from docket.config.digest import digest
from docket.transports.base import Failure, Request, Response

LOCAL_KINDS = ("replay", "files")


class RecordedReplay:
    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.served = 0
        self.fell_through = 0
        self.missing: list[str] = []

    @classmethod
    def from_run(cls, run_dir: Path, blobs: Any) -> "RecordedReplay":
        pages: dict[str, bytes] = {}
        for idx in sorted(run_dir.glob("gen-*/records/*.jsonl")):
            for ln in idx.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    row = json.loads(ln)
                    key = digest(row["query"])
                    if key not in pages and blobs.exists(row["blob"]):
                        pages[key] = blobs.get(row["blob"])
                except (ValueError, KeyError, TypeError):
                    continue  # an index row without a query or blob cannot be replayed; the source reports it
        return cls(pages)

    def wrap(self, inner: Any, kind: str) -> "RecordedTransport":
        return RecordedTransport(self, inner, kind)


class RecordedTransport:
    def __init__(self, replay: RecordedReplay, inner: Any, kind: str) -> None:
        self.replay = replay
        self.inner = inner
        self.kind = getattr(inner, "kind", kind)
        self.id = getattr(inner, "id", "recorded")
        self.local = kind in LOCAL_KINDS
        self.info: dict[str, Any] = {"name": "recorded-replay", "protocol": 1}

    async def open(self) -> Failure | None:
        if self.local and self.inner is not None:
            return await self.inner.open()
        return None

    async def request(self, req: Request) -> Response:
        body = self.replay.pages.get(req.digest)
        if body is not None:
            self.replay.served += 1
            return Response(body=body, status=200)
        if self.local and self.inner is not None:
            self.replay.fell_through += 1
            return await self.inner.request(req)
        self.replay.missing.append(req.digest[:12])
        return Response(error=Failure("no_recording", f"rehearsal: the run recorded no response for request "
                                                      f"{req.digest[:12]}; live systems are never read in rehearsal",
                                      data={"digest": req.digest}))

    async def close(self) -> None:
        if self.local and self.inner is not None:
            await self.inner.close()


_active: contextvars.ContextVar[RecordedReplay | None] = contextvars.ContextVar("docket_recorded", default=None)


def active() -> RecordedReplay | None:
    return _active.get()


@contextmanager
def replaying(rep: RecordedReplay) -> Iterator[RecordedReplay]:
    token = _active.set(rep)
    try:
        yield rep
    finally:
        _active.reset(token)
