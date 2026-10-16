"""The restricted template language (TD §3.6).

Syntax:

- ``{field}`` and ``{a.b}`` substitute a value; ``{.}`` is the current list item.
- ``{field|filter|filter:arg}`` applies filters from a closed set.
- ``{?field}...{/field}`` renders the body when the field is present and non-empty.
- ``{#list}...{/list}`` repeats the body for each element; inside, names resolve
  against the element first and the enclosing scope second.
- ``{{`` and ``}}`` are literal braces.

There are no expressions and no calls. Parsing validates every field against a
declared context (a set of dotted paths, with ``name[]`` marking a list,
``name[].sub`` a list element's field and ``name.*`` an open map). Rendering is
total: a missing value renders empty and a filter that cannot apply returns its
input unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable

FILTERS = ("upper", "lower", "title", "trim", "truncate", "date", "duration", "join",
           "default", "redact", "slug", "first", "count")
_ARG_REQUIRED = {"truncate", "date"}
_ARG_ALLOWED = {"truncate", "date", "join", "default"}
_NAME = re.compile(r"^(?:\.|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$")


class TemplateError(ValueError):
    def __init__(self, message: str, pos: int | None = None, field_name: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.field_name = field_name


@dataclass
class Text:
    text: str


@dataclass
class Var:
    path: str
    filters: list[tuple[str, str | None]]
    pos: int


@dataclass
class Block:
    kind: str  # "?" or "#"
    path: str
    body: list[Any] = field(default_factory=list)
    pos: int = 0


@dataclass
class Template:
    source: str
    nodes: list[Any]

    def fields(self) -> set[str]:
        out: set[str] = set()

        def walk(nodes: list[Any]) -> None:
            for n in nodes:
                if isinstance(n, Var):
                    out.add(n.path)
                elif isinstance(n, Block):
                    out.add(n.path)
                    walk(n.body)

        walk(self.nodes)
        return out


def _parse_filters(spec: str, pos: int) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for raw in spec.split("|"):
        name, sep, arg = raw.partition(":")
        name = name.strip()
        if name not in FILTERS:
            raise TemplateError(f"unknown filter {name!r}; filters are {', '.join(FILTERS)}", pos)
        a: str | None = arg if sep else None
        if name in _ARG_REQUIRED and not a:
            raise TemplateError(f"filter {name!r} needs an argument ({name}:...)", pos)
        if a is not None and name not in _ARG_ALLOWED:
            raise TemplateError(f"filter {name!r} takes no argument", pos)
        if name == "truncate":
            if not a.strip().isdigit() or int(a) < 1:
                raise TemplateError("truncate needs a positive integer, for example truncate:80", pos)
        out.append((name, a))
    return out


def parse(source: str) -> Template:
    """Parse a template. Raises ``TemplateError`` with the character position."""
    root: list[Any] = []
    stack: list[tuple[Block | None, list[Any]]] = [(None, root)]
    buf: list[str] = []
    i, n = 0, len(source)

    def flush() -> None:
        if buf:
            stack[-1][1].append(Text("".join(buf)))
            buf.clear()

    while i < n:
        c = source[i]
        if c == "{" and source.startswith("{{", i):
            buf.append("{")
            i += 2
            continue
        if c == "}" and source.startswith("}}", i):
            buf.append("}")
            i += 2
            continue
        if c == "}":
            raise TemplateError("unmatched '}' (write '}}' for a literal brace)", i)
        if c != "{":
            buf.append(c)
            i += 1
            continue
        end = source.find("}", i + 1)
        if end < 0:
            raise TemplateError("unclosed '{'", i)
        inner = source[i + 1:end].lstrip()
        flush()
        if not inner.strip():
            raise TemplateError("empty substitution '{}'", i)
        if inner[0] in "?#":
            name = inner[1:].strip()
            if not _NAME.match(name) or name == ".":
                raise TemplateError(f"bad block name {name!r}", i)
            blk = Block(inner[0], name, [], i)
            stack[-1][1].append(blk)
            stack.append((blk, blk.body))
        elif inner[0] == "/":
            name = inner[1:].strip()
            top = stack[-1][0]
            if top is None:
                raise TemplateError(f"closing {{/{name}}} without an open block", i)
            if top.path != name:
                raise TemplateError(f"closing {{/{name}}} does not match open block {{{top.kind}{top.path}}}", i)
            stack.pop()
        else:
            name, _, spec = inner.partition("|")
            name = name.strip()  # filter arguments keep their spaces (join: / )
            if not _NAME.match(name):
                raise TemplateError(f"bad field name {name!r}", i)
            filters = _parse_filters(spec, i) if spec else []
            stack[-1][1].append(Var(name, filters, i))
        i = end + 1
    flush()
    if len(stack) > 1:
        blk = stack[-1][0]
        raise TemplateError(f"block {{{blk.kind}{blk.path}}} is never closed", blk.pos)
    return Template(source, root)


# ---------------------------------------------------------------------------
# Context validation


class FieldSet:
    """Declared context fields: dotted paths, ``x[]`` lists, ``x[].y`` element fields, ``x.*`` open maps."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = set(paths)

    def _known(self, path: str) -> bool:
        if path in self.paths or path + "[]" in self.paths:
            return True
        parts = path.split(".")
        for k in range(1, len(parts)):
            if ".".join(parts[:k]) + ".*" in self.paths:
                return True
        # a parent of a declared path is a map and may be tested in {?x}
        return any(p.startswith(path + ".") or p.startswith(path + "[].") for p in self.paths)

    def is_list(self, path: str) -> bool:
        return path + "[]" in self.paths

    def resolve(self, name: str, scopes: list[str]) -> str | None:
        """Return the fully qualified declared path for ``name`` or None."""
        if name == ".":
            return scopes[-1] + "[]" if scopes else None
        for scope in reversed(scopes):
            cand = f"{scope}[].{name}"
            if self._known(cand):
                return cand
        return name if self._known(name) else None

    def names(self) -> list[str]:
        return sorted(p for p in self.paths)


def validate(template: str | Template, fields: FieldSet | Iterable[str]) -> list[TemplateError]:
    """Return every error: parse errors, or fields the context does not declare."""
    fs = fields if isinstance(fields, FieldSet) else FieldSet(fields)
    try:
        tpl = template if isinstance(template, Template) else parse(template)
    except TemplateError as e:
        return [e]
    errors: list[TemplateError] = []

    def walk(nodes: list[Any], scopes: list[str]) -> None:
        for nd in nodes:
            if isinstance(nd, Var):
                if fs.resolve(nd.path, scopes) is None:
                    errors.append(TemplateError(f"unknown field {nd.path!r}", nd.pos, nd.path))
            elif isinstance(nd, Block):
                q = fs.resolve(nd.path, scopes)
                if q is None:
                    errors.append(TemplateError(f"unknown field {nd.path!r}", nd.pos, nd.path))
                    continue
                if nd.kind == "#":
                    if not fs.is_list(q):
                        errors.append(TemplateError(f"{{#{nd.path}}} repeats a field that is not a list", nd.pos, nd.path))
                        continue
                    walk(nd.body, scopes + [q])
                else:
                    walk(nd.body, scopes)

    walk(tpl.nodes, [])
    return errors


# ---------------------------------------------------------------------------
# Rendering

_MISSING = object()


def _lookup(path: str, frames: list[Any]) -> Any:
    if path == ".":
        return frames[-1] if frames else None
    parts = path.split(".")
    for frame in reversed(frames):
        cur: Any = frame
        ok = True
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            elif not isinstance(cur, (dict, list, str, int, float, type(None))) and hasattr(cur, p):
                cur = getattr(cur, p)
            else:
                ok = False
                break
        if ok:
            return cur
    return None


def _empty(v: Any) -> bool:
    return v is None or v is False or v is _MISSING or v == "" or (isinstance(v, (list, dict, tuple)) and not v)


def _to_text(v: Any) -> str:
    if _empty(v):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, tuple)):
        return ", ".join(_to_text(x) for x in v)
    return str(v)


def _as_date(v: Any) -> date | datetime | None:
    if isinstance(v, (date, datetime)):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")) if "T" in v else date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _apply_filter(name: str, arg: str | None, v: Any, redactor: Callable[[str], str] | None) -> Any:
    try:
        if name == "default":
            return (arg or "") if _empty(v) else v
        if name == "count":
            return len(v) if isinstance(v, (list, tuple, dict, str)) else (0 if _empty(v) else 1)
        if name == "first":
            if isinstance(v, (list, tuple)):
                return v[0] if v else None
            return v
        if name == "join":
            if isinstance(v, (list, tuple)):
                return (", " if arg is None else arg).join(_to_text(x) for x in v)
            return v
        if name == "duration":
            if _empty(v):
                return v
            from docket.text.durations import format_duration, parse_duration
            return format_duration(parse_duration(v))
        if name == "date":
            d = _as_date(v)
            return d.strftime(arg or "%Y-%m-%d") if d is not None else v
        s = _to_text(v)
        if name == "upper":
            return s.upper()
        if name == "lower":
            return s.lower()
        if name == "title":
            return s.title()
        if name == "trim":
            return s.strip()
        if name == "truncate":
            lim = int(arg or "0")
            return s if len(s) <= lim else s[: max(lim - 1, 0)].rstrip() + "\u2026"
        if name == "slug":
            return _slug(s)
        if name == "redact":
            if not s:
                return s
            return redactor(s) if redactor else "[redacted]"
    except Exception:  # rendering is total: a filter that cannot apply passes the value through
        return v
    return v


def render(template: str | Template, context: dict[str, Any], *, redactor: Callable[[str], str] | None = None) -> str:
    """Render a template. Never raises on data; a missing value renders empty."""
    tpl = template if isinstance(template, Template) else parse(template)
    out: list[str] = []

    def walk(nodes: list[Any], frames: list[Any]) -> None:
        for nd in nodes:
            if isinstance(nd, Text):
                out.append(nd.text)
            elif isinstance(nd, Var):
                v = _lookup(nd.path, frames)
                for fname, farg in nd.filters:
                    v = _apply_filter(fname, farg, v, redactor)
                out.append(_to_text(v))
            elif isinstance(nd, Block):
                v = _lookup(nd.path, frames)
                if nd.kind == "?":
                    if not _empty(v):
                        walk(nd.body, frames)
                else:
                    if isinstance(v, (list, tuple)):
                        for el in v:
                            walk(nd.body, frames + [el])
                    elif not _empty(v):
                        walk(nd.body, frames + [v])

    walk(tpl.nodes, [context])
    return "".join(out)
