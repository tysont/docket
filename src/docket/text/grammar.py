"""The configurable task-line grammar (TD §11.3).

A task line is ``<indent><status marker><text><fields>``:

- the status marker is the configured ``open`` string or one of the ``done``
  strings (the first is written when closing). When ``open`` is a Markdown
  checkbox, any other single-character state (``- [-]``, ``- [>]``) parses with
  status ``other`` and is never rewritten;
- fields are a trailing run in one of three styles: ``bracket``
  (``[key:: value]``), ``inline`` (``key:: value``) or ``delimited``
  (``<separator>key<assign>value``, for example `` | queue=tier1``);
- links (Markdown ``[label](url)``, wiki ``[[target]]``, bare URLs, and field
  values that are URLs) and tags (``#word``) are read from the text.

``parse`` returns None for a line that is not a task under the grammar.
``render(parse(line)) == line`` for every line in canonical form (single
spaces before bracketed and inline fields). Edits to an existing line go
through ``set_field``, ``close`` and ``with_text``, which change only the
part they name, so a hand-formatted line keeps its formatting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

STYLES = ("bracket", "inline", "delimited")
_KEY = r"[A-Za-z_][A-Za-z0-9_-]*"
_BRACKET_FIELD = re.compile(r" ?\[(" + _KEY + r"):: ([^\]]*)\]$")
_INLINE_START = re.compile(r"(?:^|\s)(" + _KEY + r"):: ")
_MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_WIKI = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_URL = re.compile(r"(?<![(\w])https?://[^\s)\]>]+")
_TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/-]*)")
_CHECKBOX = re.compile(r"^(- \[)(.)(\] )")


@dataclass(frozen=True)
class TaskLine:
    status: str  # open | done | other
    text: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    indent: str = ""
    marker: str = ""  # the exact status marker read (kept for render)

    def get(self, key: str) -> str | None:
        for k, v in self.fields:
            if k == key:
                return v
        return None

    @property
    def links(self) -> list[str]:
        out: list[str] = []
        for m in _MD_LINK.finditer(self.text):
            out.append(m.group(1))
        for m in _WIKI.finditer(self.text):
            out.append(m.group(1).strip())
        stripped = _MD_LINK.sub(" ", self.text)
        for m in _URL.finditer(stripped):
            out.append(m.group(0))
        for _, v in self.fields:
            if re.match(r"^https?://\S+$", v.strip()):
                out.append(v.strip())
        return out

    @property
    def tags(self) -> list[str]:
        return [m.group(1) for m in _TAG.finditer(_MD_LINK.sub(" ", self.text))]

    def with_text(self, text: str) -> "TaskLine":
        return replace(self, text=text)

    def with_field(self, key: str, value: str) -> "TaskLine":
        fields = list(self.fields)
        for i, (k, _) in enumerate(fields):
            if k == key:
                fields[i] = (key, value)
                return replace(self, fields=fields)
        return replace(self, fields=fields + [(key, value)])


@dataclass(frozen=True)
class Grammar:
    open: str = "- [ ] "
    done: tuple[str, ...] = ("- [x] ", "- [X] ")
    fields: str = "bracket"
    separator: str = " | "
    assign: str = "="

    @classmethod
    def from_config(cls, cfg: Any) -> "Grammar":
        if cfg is None:
            return cls()
        d = cfg if isinstance(cfg, dict) else cfg.model_dump()
        d = {k: v for k, v in d.items() if v is not None}
        style = d.get("fields", "bracket")
        if style not in STYLES:
            raise ValueError(f"unknown field style {style!r}; expected one of {', '.join(STYLES)}")
        done = d.get("done", cls.done)
        if isinstance(done, str):
            done = (done,)
        g = cls(open=d.get("open", cls.open), done=tuple(done), fields=style,
                separator=d.get("separator", cls.separator), assign=d.get("assign", cls.assign))
        if not g.open or not g.done or not all(g.done):
            raise ValueError("grammar open and done markers must be non-empty")
        return g

    # --- status markers ----------------------------------------------------
    @property
    def _checkbox(self) -> bool:
        return bool(_CHECKBOX.match(self.open))

    def _split_marker(self, body: str) -> tuple[str, str] | None:
        if body.startswith(self.open):
            return "open", self.open
        for d in self.done:
            if body.startswith(d):
                return "done", d
        if self._checkbox:
            m = _CHECKBOX.match(body)
            if m:
                return "other", m.group(0)
        return None

    # --- fields ------------------------------------------------------------
    def _split_fields(self, rest: str) -> tuple[str, list[tuple[str, str]]]:
        if self.fields == "bracket":
            fields: list[tuple[str, str]] = []
            text = rest
            while True:
                m = _BRACKET_FIELD.search(text)
                if not m:
                    break
                fields.insert(0, (m.group(1), m.group(2)))
                text = text[: m.start()]
            return text, fields
        if self.fields == "inline":
            starts = list(_INLINE_START.finditer(rest))
            if not starts:
                return rest, []
            first = starts[0]
            text = rest[: first.start()]
            out = []
            for i, m in enumerate(starts):
                end = starts[i + 1].start() if i + 1 < len(starts) else len(rest)
                out.append((m.group(1), rest[m.end(): end]))
            return text, out
        parts = rest.split(self.separator)
        fields = []
        while len(parts) > 1:
            seg = parts[-1]
            key, sep, val = seg.partition(self.assign)
            if not sep or not re.fullmatch(_KEY, key):
                break
            fields.insert(0, (key, val))
            parts.pop()
        return self.separator.join(parts), fields

    def _render_fields(self, fields: list[tuple[str, str]]) -> str:
        if self.fields == "bracket":
            return "".join(f" [{k}:: {v}]" for k, v in fields)
        if self.fields == "inline":
            return "".join(f" {k}:: {v}" for k, v in fields)
        return "".join(f"{self.separator}{k}{self.assign}{v}" for k, v in fields)

    # --- public API --------------------------------------------------------
    def parse(self, line: str) -> TaskLine | None:
        body = line.rstrip("\r\n")
        indent = body[: len(body) - len(body.lstrip(" \t"))]
        body = body[len(indent):]
        sm = self._split_marker(body)
        if sm is None:
            return None
        status, marker = sm
        text, fields = self._split_fields(body[len(marker):])
        return TaskLine(status=status, text=text, fields=fields, indent=indent, marker=marker)

    def render(self, t: TaskLine) -> str:
        marker = t.marker or (self.open if t.status == "open" else self.done[0])
        if t.status == "open" and t.marker not in ("", self.open):
            marker = self.open
        if t.status == "done" and t.marker not in self.done:
            marker = self.done[0]
        return f"{t.indent}{marker}{t.text}{self._render_fields(t.fields)}"

    def new(self, text: str, fields: list[tuple[str, str]] | None = None) -> TaskLine:
        return TaskLine(status="open", text=text, fields=list(fields or []), marker=self.open)

    def is_task(self, line: str) -> bool:
        return self.parse(line) is not None

    def set_field(self, line: str, key: str, value: str) -> str:
        """Set one field, in place when present, else appended; nothing else on the line changes."""
        t = self.parse(line)
        if t is None:
            raise ValueError("not a task line under this grammar")
        prefix = f"{t.indent}{t.marker}{t.text}"
        if t.get(key) is None:
            return line.rstrip("\r\n") + self._render_fields([(key, value)])
        return prefix + self._render_fields([(k, value if k == key else v) for k, v in t.fields])

    def close(self, line: str) -> str:
        t = self.parse(line)
        if t is None:
            raise ValueError("not a task line under this grammar")
        if t.status != "open":
            return line
        head = t.indent + t.marker
        return t.indent + self.done[0] + line[len(head):]
