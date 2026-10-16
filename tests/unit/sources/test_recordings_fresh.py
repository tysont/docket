"""The persona recordings match what the presets' request plans ask for today (TP §7.1).

A preset change that alters a request makes the committed recordings stale; regenerate
them with ``uv run python tests/fixtures/make_recordings.py``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.mark.parametrize("persona", ["reference-like", "support-lead"])
def test_recordings_are_fresh(tmp_path, persona):
    spec = importlib.util.spec_from_file_location("make_recordings", FIXTURES / "make_recordings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = mod.record(persona, tmp_path)
    for name, path in fresh.items():
        committed = FIXTURES / "personas" / persona / "recordings" / f"{name}.jsonl"
        assert committed.read_text() == path.read_text(), f"{committed} is stale; run tests/fixtures/make_recordings.py"
