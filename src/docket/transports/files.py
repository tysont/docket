"""The ``files`` transport: JSON-lines files under a root directory.

Request payload: ``{"path": "<relative path>", "args": {"offset": N, "limit": N}}``.
The response body is JSON: ``{"entries": [...], "next": "<line offset>", "end": bool}``,
each entry the parsed line, or the raw text of a line that is not JSON (the
source's contract check records that shape). Blank lines count toward the
offset and yield no entry. The offset is the count of lines consumed, so a
source with ``cursor.position: token`` resumes after the last line it read.

``read_jsonl`` is the M1 helper kept for callers that read a whole file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docket.transports.base import Failure, Request, Response


@dataclass
class FileRead:
    path: Path
    exists: bool
    total_lines: int = 0
    lines: list[tuple[int, bytes]] = field(default_factory=list)  # (1-based line number, raw bytes)


def read_jsonl(path: Path, start: int = 0) -> FileRead:
    if not path.is_file():
        return FileRead(path, False)
    raw = path.read_bytes().splitlines()
    out = FileRead(path, True, total_lines=len(raw))
    for n, line in enumerate(raw, 1):
        if n <= start or not line.strip():
            continue
        out.lines.append((n, line))
    return out


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class FilesTransport:
    kind = "files"

    def __init__(self, tid: str, root: Path) -> None:
        self.id = tid
        self.root = root
        self.info: dict[str, Any] = {"name": "files", "protocol": 1}

    async def open(self) -> Failure | None:
        return None

    async def request(self, req: Request) -> Response:
        rel = req.payload.get("path")
        if not isinstance(rel, str) or not rel:
            return Response(error=Failure("protocol", "files request has no path"))
        path = (self.root / rel)
        if not path.is_file():
            return Response(error=Failure("unavailable", f"file not found: {rel}"))
        args = req.payload.get("args") or {}
        offset = max(0, _int(args.get("offset"), 0))
        limit = max(1, _int(args.get("limit"), 500))
        lines = path.read_bytes().splitlines()
        entries: list[Any] = []
        n = offset
        while n < len(lines) and len(entries) < limit:
            raw = lines[n]
            n += 1
            if not raw.strip():
                continue
            try:
                entries.append(json.loads(raw))
            except ValueError:
                entries.append(raw.decode("utf-8", "replace"))
        body = json.dumps({"entries": entries, "next": str(n), "end": n >= len(lines)},
                          sort_keys=True, ensure_ascii=False).encode("utf-8")
        return Response(body=body)

    async def close(self) -> None:
        return None
