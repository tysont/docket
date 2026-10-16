"""Adapter conformance (TD §17.3, Test Plan §7.3): every built-in adapter and its twin."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from docket.config.models import Destination as DestConfig
from docket.destinations import conformance

STUB = Path(__file__).resolve().parents[1] / "fixtures" / "bin" / "calbridge_stub.py"
IN_PROCESS = ["markdown_tasks", "markdown_agenda", "journal", "calendar_file", "calendar_mcp", "message_mcp"]
EXPECTED = {"capabilities", "preview_determinism", "twin_no_effect", "apply_and_verify", "idempotent_apply",
            "twin_parity", "unowned_refused", "capabilities_match"}


def _assert_passes(rep: conformance.Report, *, allow_skipped: set[str] = frozenset()) -> None:
    assert {c.name for c in rep.checks} == EXPECTED
    failed = [c.as_dict() for c in rep.checks if not c.ok]
    assert not failed, failed
    skipped = {c.name for c in rep.checks if c.skipped}
    assert skipped <= allow_skipped, skipped


@pytest.mark.parametrize("adapter", IN_PROCESS)
def test_builtin_adapter_conforms(adapter, tmp_path):
    rep = conformance.check(adapter, conformance.builtin_config(adapter), tmp_path, f"d-{adapter}")
    _assert_passes(rep, allow_skipped={"unowned_refused"} if adapter == "message_mcp" else set())


def _exec_cfg(tmp_path: Path, *flags: str) -> DestConfig:
    return DestConfig.model_validate({"adapter": "calendar_exec", "visibility": "shared", "calendar": "work",
                                      "program": [sys.executable, str(STUB), "--state",
                                                  str(tmp_path / "state.json"), *flags]})


@pytest.mark.parametrize("flags", [(), ("--no-sandbox",)], ids=["program-twin", "generic-twin"])
def test_exec_adapter_conforms_on_scratch_target(tmp_path, flags):
    rep = conformance.check("calendar_exec", _exec_cfg(tmp_path, *flags), tmp_path, "cal", root=tmp_path)
    _assert_passes(rep)
    assert not (tmp_path / "state.json").exists()  # the live state was never touched


def test_exec_program_without_scratch_runs_handshake_only(tmp_path):
    rep = conformance.check("calendar_exec", _exec_cfg(tmp_path, "--no-scratch"), tmp_path, "cal", root=tmp_path)
    assert rep.ok
    skipped = {c.name for c in rep.checks if c.skipped}
    assert skipped == EXPECTED - {"capabilities"}
    assert not (tmp_path / "state.json").exists()


def test_capability_declarations_follow_the_program(tmp_path):
    cfg = _exec_cfg(tmp_path, "--caps", "list,create,delete")
    rep = conformance.check("calendar_exec", cfg, tmp_path, "cal", root=tmp_path)
    caps = next(c for c in rep.checks if c.name == "capabilities")
    assert "calendar.place" in caps.detail and "calendar.move" not in caps.detail
    # the scenario uses move, which this program does not declare: conformance must notice
    assert not next(c for c in rep.checks if c.name == "apply_and_verify").ok


def test_a_broken_adapter_fails_conformance(tmp_path, monkeypatch):
    from docket.destinations.markdown import MarkdownTasks
    real = MarkdownTasks.apply

    def not_idempotent(self, op):
        rec = real(self, op)
        return rec.model_copy(update={"outcome": "applied"})

    monkeypatch.setattr(MarkdownTasks, "apply", not_idempotent)
    rep = conformance.check("markdown_tasks", conformance.builtin_config("markdown_tasks"), tmp_path, "t")
    assert not rep.ok
    assert not next(c for c in rep.checks if c.name == "idempotent_apply").ok
