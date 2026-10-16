"""Portability (NFR-07): core code has no platform branch; platform adapters are optional and isolated.

A native calendar bridge reaches Docket only through the exec protocol (``calendar_exec``): a
program the workspace names, started on demand. No module branches on the operating system
or imports a platform-only library, and adapters load lazily from the registry, so a workspace
that configures no platform adapter never imports one.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from docket.destinations import ADAPTERS

pytestmark = pytest.mark.smoke
SRC = Path(__file__).resolve().parents[2] / "src" / "docket"
PLATFORM_MODULES = {"fcntl", "termios", "pwd", "grp", "winreg", "msvcrt", "_winapi", "objc", "Foundation",
                    "EventKit", "AppKit"}
PLATFORM_TEXT = re.compile(r"sys\.platform|platform\.system\(|os\.uname\(|\bosascript\b")


def test_no_platform_branches_or_platform_only_imports():
    found = []
    for f in sorted(SRC.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        if PLATFORM_TEXT.search(text):
            found.append(f"{f.relative_to(SRC)}: platform branch")
        for node in ast.walk(ast.parse(text)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            found += [f"{f.relative_to(SRC)}: imports {n}" for n in names if n in PLATFORM_MODULES]
    assert found == []


def test_adapters_load_lazily_from_the_registry():
    assert all(isinstance(v, str) and ":" in v for v in ADAPTERS.values())
    init = (SRC / "destinations" / "__init__.py").read_text(encoding="utf-8")
    assert "calendar_exec import" not in init and "import calendar_exec" not in init
