"""Markdown I/O for the Markdown adapters: the swappable file layer and section editing.

``FileIO`` reads and writes files under a root. ``TwinIO`` is the sandbox
layer: it writes under the twin root and, the first time it touches a path,
seeds it with a copy of the live file. Adapters see only the ``IO`` interface,
so the twin runs the same preview, apply and verify code (TD §11.1).

``Doc`` keeps a file's newline convention and trailing newline, finds a
section by its exact heading line (a heading of the same or a higher level
ends it; fenced code is skipped) and edits lines inside it.
"""
from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass
from pathlib import Path

from docket.config.digest import sha256_hex
from docket.store.paths import write_atomic


def line_digest(line: str) -> str:
    """The identity of a line for replace operations: trailing whitespace is not part of it."""
    return sha256_hex(line.rstrip().encode("utf-8"))


class FileIO:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    def read(self, rel: str) -> str | None:
        p = self.path(rel)
        return p.read_bytes().decode("utf-8") if p.is_file() else None

    def write(self, rel: str, text: str) -> None:
        write_atomic(self.path(rel), text)

    def glob(self, base: str, pattern: str) -> list[str]:
        d = self.root / base if base else self.root
        if not d.is_dir():
            return []
        out = []
        for p in d.rglob("*"):
            if p.is_file():
                rel = p.relative_to(d).as_posix()
                if fnmatch.fnmatch(rel, pattern):
                    out.append(((Path(base) / rel).as_posix()) if base else rel)
        return sorted(out)


class TwinIO(FileIO):
    """Writes under ``root``; seeds each file from ``live`` on first touch. Never writes to ``live``."""

    def __init__(self, root: Path, live: FileIO) -> None:
        super().__init__(root)
        self.live = live

    def _seed(self, rel: str) -> None:
        mine = super().path(rel)
        if mine.exists():
            return
        src = self.live.path(rel)
        if src.is_file():
            mine.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, mine)

    def path(self, rel: str) -> Path:
        self._seed(rel)
        return super().path(rel)

    def glob(self, base: str, pattern: str) -> list[str]:
        return sorted(set(self.live.glob(base, pattern)) | set(super().glob(base, pattern)))


def heading_level(line: str) -> int:
    s = line.lstrip()
    if not s.startswith("#"):
        return 0
    n = len(s) - len(s.lstrip("#"))
    return n if n <= 6 and s[n:n + 1] in (" ", "") else 0


@dataclass
class Doc:
    lines: list[str]
    newline: str = "\n"
    trailing: bool = True

    @classmethod
    def parse(cls, text: str | None) -> "Doc":
        if not text:
            return cls([], "\n", True)
        nl = "\r\n" if "\r\n" in text else "\n"
        trailing = text.endswith(nl)
        body = text[: -len(nl)] if trailing else text
        return cls(body.split(nl), nl, trailing)

    def render(self) -> str:
        if not self.lines:
            return ""
        return self.newline.join(self.lines) + (self.newline if self.trailing else "")

    def headings(self) -> list[tuple[int, int]]:
        """(index, level) of every heading outside fenced code."""
        out, fence = [], False
        for i, ln in enumerate(self.lines):
            if ln.lstrip().startswith(("```", "~~~")):
                fence = not fence
                continue
            if not fence:
                lv = heading_level(ln)
                if lv:
                    out.append((i, lv))
        return out

    def section(self, anchor: str) -> tuple[int, int] | None:
        """(heading index, end index) of the first section whose heading line equals ``anchor``."""
        hs = self.headings()
        want = anchor.strip()
        for n, (i, lv) in enumerate(hs):
            if self.lines[i].strip() == want:
                end = len(self.lines)
                for j, lj in hs[n + 1:]:
                    if lj <= lv:
                        end = j
                        break
                return i, end
        return None

    def body(self, anchor: str) -> list[tuple[int, str]] | None:
        sec = self.section(anchor)
        if sec is None:
            return None
        return [(i, self.lines[i]) for i in range(sec[0] + 1, sec[1])]

    def section_of(self, index: int) -> str | None:
        """The heading line of the innermost section containing ``index``."""
        best = None
        for i, _ in self.headings():
            if i < index:
                best = self.lines[i]
        return best

    def add_section(self, anchor: str) -> tuple[int, int]:
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.append(anchor)
        return len(self.lines) - 1, len(self.lines)

    def append_in(self, anchor: str, line: str) -> None:
        start, end = self.section(anchor) or self.add_section(anchor)
        ins = end
        while ins > start + 1 and not self.lines[ins - 1].strip():
            ins -= 1
        self.lines.insert(ins, line)

    def matches(self, anchor: str, old_digest: str) -> list[int]:
        rows = self.body(anchor) or []
        return [i for i, ln in rows if line_digest(ln) == old_digest]

    def count_in(self, anchor: str, line: str) -> int:
        rows = self.body(anchor) or []
        return sum(1 for _, ln in rows if ln.rstrip() == line.rstrip())
