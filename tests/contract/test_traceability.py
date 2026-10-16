"""Requirement traceability (Test Plan §9, IP §7).

Requirement ids are parsed from the PRD itself. Every Must, and every Should the
Implementation Plan §5 does not defer, maps to at least one existing test in
``tests/traceability.yaml``. Non-functional requirements carry no priority marker and
count as Must. A deferred requirement names its reason; a mapped test must exist.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parents[2]
PRD = ROOT / "docs" / "Docket Product Requirements.md"
PLAN = ROOT / "docs" / "Docket Implementation Plan.md"
MAP = ROOT / "tests" / "traceability.yaml"
REQ = re.compile(r"^- \*\*((?:FR|AE|CFG|NFR)-[A-Z0-9-]+?)(?: \((Must|Should|Could)\))?[ .]", re.M)


def requirements() -> dict[str, str]:
    """Requirement id -> priority (``Must`` for the unmarked non-functional requirements)."""
    out = {}
    for rid, prio in REQ.findall(PRD.read_text(encoding="utf-8")):
        out[rid] = prio or "Must"
    return out


def deferred_in_plan() -> dict[str, str]:
    text = PLAN.read_text(encoding="utf-8")
    section = text.split("## 5. Deferred PRD items", 1)[1].split("\n## ", 1)[0]
    out = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*((?:FR|AE|CFG|NFR)-[A-Z0-9-]+)[^|]*\|\s*([^|]+)\|", line)
        if m and "deferred" in m.group(2).lower():
            out[m.group(1)] = m.group(2).strip()
    return out


def mapping() -> dict[str, dict]:
    raw = YAML(typ="safe").load(MAP.read_text(encoding="utf-8"))
    out = {}
    for rid, v in raw.items():
        out[rid] = {"tests": list(v), "deferred": None} if isinstance(v, list) else \
            {"tests": list(v.get("tests") or []), "deferred": v.get("deferred")}
    return out


def existing_tests() -> set[str]:
    out = set()
    for f in sorted((ROOT / "tests").rglob("test_*.py")):
        rel = f.relative_to(ROOT).as_posix()
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                out.add(f"{rel}::{node.name}")
    return out


def test_the_prd_parses():
    reqs = requirements()
    assert len(reqs) >= 80 and reqs["FR-SRC-01"] == "Must" and reqs["AE-10"] == "Should" and reqs["NFR-03"] == "Must"
    assert set(deferred_in_plan()) == {"FR-ACT-05", "FR-SRC-11"}


def test_every_required_requirement_maps_to_an_existing_test():
    reqs, mapped, deferred, tests = requirements(), mapping(), deferred_in_plan(), existing_tests()
    problems = []
    for rid in sorted(set(mapped) - set(reqs)):
        problems.append(f"{rid}: not a requirement in the PRD")
    for rid, prio in sorted(reqs.items()):
        row = mapped.get(rid)
        if row is None:
            problems.append(f"{rid} ({prio}): no entry in tests/traceability.yaml")
            continue
        for t in row["tests"]:
            if t not in tests:
                problems.append(f"{rid}: mapped test {t} does not exist")
        if row["deferred"]:
            if rid not in deferred:
                problems.append(f"{rid}: marked deferred, but the Implementation Plan §5 does not defer it")
            continue
        if rid in deferred:
            problems.append(f"{rid}: the Implementation Plan defers it; give the reason under deferred:")
        if prio in ("Must", "Should") and not row["tests"]:
            problems.append(f"{rid} ({prio}): no mapped test")
    assert problems == [], "\n".join(problems)


def test_coverage_numbers():
    reqs, mapped = requirements(), mapping()
    covered = sum(1 for r in reqs if mapped.get(r, {}).get("tests") and not mapped[r]["deferred"])
    deferred = sum(1 for r in reqs if mapped.get(r, {}).get("deferred"))
    print(f"traceability: {len(reqs)} requirements, {covered} mapped, {deferred} deferred")
    assert covered + deferred == len(reqs)
