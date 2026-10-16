"""Integrity versus shape in code (Test Plan §5.1, TD §14.1).

A static scan of the data-handling packages. Every ``raise`` must raise a
``DocketError`` subclass with a code from the error catalog whose kind matches
the class (``IntegrityError`` for data integrity; ``InputError``, ``StateError``,
``ConfigError``, ``ExternalError`` and ``LeaseError`` where the condition is one
of those), or call a local helper whose return annotation is such a class, or
re-raise the exception being handled. A genuine programming error (assert-like)
is allowed only with a ``# raise-ok: <reason>`` comment on the raise line or the
line above it. A shape (a field, a kind, a format the code has not modelled)
must take a safe reading and call ``unexpected()`` instead.

A second check: every ``unexpected()`` call names a non-empty literal ``extend``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from docket.cli.catalog import ERRORS

pytestmark = pytest.mark.smoke

SRC = Path(__file__).resolve().parents[2] / "src" / "docket"
DATA_PACKAGES = ("sources", "transports", "items", "signals", "tasks", "decide", "destinations", "plan", "apply",
                 "close")
ERROR_CLASSES = {"IntegrityError": "integrity", "InputError": "input", "StateError": "state",
                 "ConfigError": "config", "ExternalError": "external", "LeaseError": "lease"}


def _modules(packages: tuple[str, ...] = DATA_PACKAGES) -> list[Path]:
    out: list[Path] = []
    for p in packages:
        out += sorted((SRC / p).rglob("*.py"))
    return out


def _name(fn: ast.expr) -> str | None:
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _helpers(tree: ast.Module) -> dict[str, str | None]:
    """Function and method name -> the error class its return annotation names (or None)."""
    out: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ann = node.returns
            text = ast.unparse(ann).strip("'\"") if ann is not None else ""
            out[node.name] = text if text in ERROR_CLASSES or text == "DocketError" else None
    return out


def _marked(lines: list[str], node: ast.Raise) -> bool:
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    span = range(max(1, node.lineno - 1), end + 1)
    return any("# raise-ok:" in lines[i - 1] and lines[i - 1].split("# raise-ok:", 1)[1].strip() for i in span)


def _check_construction(call: ast.Call, where: str) -> str | None:
    cls = _name(call.func)
    if cls not in ERROR_CLASSES:
        return None
    if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
        return f"{where}: {cls} needs a literal catalog code as its first argument"
    code = call.args[0].value
    if code not in ERRORS:
        return f"{where}: {cls}({code!r}): the code is not in the error catalog"
    if ERRORS[code][0] != ERROR_CLASSES[cls]:
        return f"{where}: {cls}({code!r}): the catalog kind is {ERRORS[code][0]!r}"
    return None


def _global_helpers() -> dict[str, str | None]:
    """Helpers across the data packages: a name counts when every definition of it returns an error class."""
    out: dict[str, str | None] = {}
    for f in _modules():
        for name, cls in _helpers(ast.parse(f.read_text(encoding="utf-8"), str(f))).items():
            out[name] = cls if out.get(name, cls) is not None else None
    return out


def violations(path: Path, shared: dict[str, str | None] | None = None) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text, str(path))
    helpers = {**(shared or {}), **_helpers(tree)}
    rel = path.relative_to(SRC.parent) if path.is_relative_to(SRC.parent) else path.name
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            bad = _check_construction(node, f"{rel}:{node.lineno}")
            if bad:
                out.append(bad)
        if not isinstance(node, ast.Raise):
            continue
        where = f"{rel}:{node.lineno}"
        exc = node.exc
        if exc is None or _marked(lines, node):
            continue  # a bare re-raise of the handled exception, or a marked programming error
        if isinstance(exc, ast.Call):
            name = _name(exc.func)
            if name in ERROR_CLASSES:
                continue  # the construction itself is checked above
            if name in helpers and helpers[name] is not None:
                continue
            out.append(f"{where}: raise {ast.unparse(exc.func)}(...) is not a catalogued DocketError; take a safe "
                       f"reading with unexpected(), raise a catalogued error, or mark it '# raise-ok: <reason>'")
        else:
            out.append(f"{where}: raise {ast.unparse(exc)} re-raises a stored exception; mark it '# raise-ok: <reason>'")
    return out


def test_data_packages_raise_only_catalogued_errors():
    found: list[str] = []
    shared = _global_helpers()
    for f in _modules():
        found += violations(f, shared)
    assert found == [], "\n".join(found)


def test_every_error_construction_in_src_uses_a_catalogued_code():
    found: list[str] = []
    for f in sorted(SRC.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in ERROR_CLASSES and node.args \
                    and isinstance(node.args[0], ast.Constant):
                bad = _check_construction(node, f"{f.relative_to(SRC.parent)}:{node.lineno}")
                if bad:
                    found.append(bad)
    assert found == [], "\n".join(found)


def test_the_scan_catches_an_unmarked_raise_and_a_wrong_code(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("from docket.cli.errors import IntegrityError, InputError\n"
                     "def f(row):\n    raise ValueError(row)\n"
                     "def g(row):\n    raise ValueError(row)  # raise-ok: assert-like\n"
                     "def h(row):\n    raise IntegrityError('input.bad_date', 'x')\n"
                     "def k(row):\n    raise InputError('no.such.code', 'x')\n"
                     "def m() -> IntegrityError:\n    return IntegrityError('dest.unowned', 'x')\n"
                     "def n():\n    raise m()\n")
    found = violations(probe)
    assert len(found) == 3, found
    assert "probe.py:3" in found[0] or any("probe.py:3" in x for x in found)
    assert any("catalog kind" in x for x in found) and any("not in the error catalog" in x for x in found)


def test_every_unexpected_call_names_a_literal_extend():
    calls = 0
    for f in sorted(SRC.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) == "unexpected" and \
                    not isinstance(node.func, ast.Attribute):
                calls += 1
                kw = {k.arg: k.value for k in node.keywords}
                where = f"{f.relative_to(SRC.parent)}:{node.lineno}"
                assert "extend" in kw, f"{where}: unexpected() without extend"
                v = kw["extend"]
                if isinstance(v, ast.Constant):
                    assert isinstance(v.value, str) and v.value.strip(), f"{where}: empty extend"
                else:
                    assert isinstance(v, ast.JoinedStr), f"{where}: extend must be a literal or an f-string"
                    literal = "".join(p.value for p in v.values if isinstance(p, ast.Constant))
                    named = any(isinstance(p, ast.FormattedValue) and isinstance(p.value, ast.Call)
                                and "extend" in (_name(p.value.func) or "") for p in v.values)
                    assert literal.strip() or named, \
                        f"{where}: extend f-string has no literal text naming the extension point"
    assert calls >= 20
