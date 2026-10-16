"""The ``files`` transport, M1 form: a JSON-lines file per source.

A transport returns bodies exactly as stored and knows nothing about records,
cursors or items. Position is the count of lines already consumed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
