"""The ``markdown_tasks`` adapter, M1 form (TD §11.1, §11.3).

Supports the ``append`` verb: add one line at the end of an anchor section.
Apply is idempotent by content: a line already present in the section is not
added again, so a crash between the write and the receipt cannot duplicate it.
The sandbox twin is the same adapter rooted at the run's sandbox directory and
seeded with a copy of the live file.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from docket.config.digest import digest, sha256_hex
from docket.config.models import Destination
from docket.shapes.unexpected import unexpected
from docket.store.paths import write_atomic


def _level(line: str) -> int:
    s = line.lstrip()
    if not s.startswith("#"):
        return 0
    n = len(s) - len(s.lstrip("#"))
    return n if s[n:n + 1] in (" ", "") else 0


class MarkdownTasks:
    def __init__(self, dest_id: str, cfg: Destination, root: Path) -> None:
        self.id = dest_id
        self.cfg = cfg
        self.root = root
        self.visibility = cfg.visibility

    @property
    def target(self) -> Path:
        return self.root / (self.cfg.path or f"{self.id}.md")

    def snapshot(self) -> str:
        return sha256_hex(self.target.read_bytes()) if self.target.is_file() else "absent"

    def sandbox(self, sandbox_root: Path) -> "MarkdownTasks":
        twin = MarkdownTasks(self.id, self.cfg, sandbox_root / self.id)
        if not twin.target.exists():
            twin.target.parent.mkdir(parents=True, exist_ok=True)
            if self.target.is_file():
                shutil.copyfile(self.target, twin.target)
        return twin

    def _section(self, lines: list[str], anchor: str) -> tuple[int, int] | None:
        for i, ln in enumerate(lines):
            if ln.strip() == anchor.strip():
                lvl = _level(ln) or 99
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    lj = _level(lines[j])
                    if lj and lj <= lvl:
                        end = j
                        break
                return i, end
        return None

    def apply(self, op: dict[str, Any]) -> dict[str, Any]:
        text = self.target.read_text(encoding="utf-8") if self.target.is_file() else ""
        lines = text.splitlines()
        anchor, new = op["anchor"], op["line"]
        sec = self._section(lines, anchor)
        if sec is None:
            unexpected("dest.markdown", "anchor section missing", sample=anchor,
                       reading="append the section at the end of the file",
                       extend=f"destinations.{self.id}.section")
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(anchor)
            sec = (len(lines) - 1, len(lines))
        start, end = sec
        if new in lines[start + 1:end]:
            return {"outcome": "already_present", "external_id": self._ext(new)}
        ins = end
        while ins > start + 1 and not lines[ins - 1].strip():
            ins -= 1
        lines.insert(ins, new)
        write_atomic(self.target, "\n".join(lines) + "\n")
        return {"outcome": "applied", "external_id": self._ext(new)}

    def verify(self, op: dict[str, Any]) -> bool:
        if not self.target.is_file():
            return False
        lines = self.target.read_text(encoding="utf-8").splitlines()
        sec = self._section(lines, op["anchor"])
        return bool(sec) and op["line"] in lines[sec[0] + 1:sec[1]]

    def _ext(self, line: str) -> str:
        return f"{self.cfg.path or self.id}#{digest(line)[:12]}"
