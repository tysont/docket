"""Every unexpected() call in src names a non-empty extension point (Test Plan §5.1)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke
SRC = Path(__file__).resolve().parents[2] / "src" / "docket"


def test_every_unexpected_call_names_extend():
    calls = 0
    for f in SRC.rglob("*.py"):
        tree = ast.parse(f.read_text(), str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "unexpected":
                calls += 1
                kw = {k.arg: k.value for k in node.keywords}
                assert "extend" in kw and "reading" in kw, f"{f}:{node.lineno}"
                v = kw["extend"]
                if isinstance(v, ast.Constant):
                    assert str(v.value).strip(), f"{f}:{node.lineno}"
                else:
                    assert isinstance(v, ast.JoinedStr) and v.values, f"{f}:{node.lineno}"
    assert calls >= 5
