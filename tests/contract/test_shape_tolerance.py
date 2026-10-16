"""Shape tolerance (Test Plan §5.6, TD §14.1, FR-SRC-07, FR-ROB-01).

Variants generated from each persona's recorded responses (``tests/fixtures/shapes``) run
through collection in process. For every variant:

- the run continues past the affected record or unit (collect succeeds);
- the expected safe reading is taken (the affected unit ends as the contract says);
- every new unexpected shape is one the variant may record, the one it must record is
  there, and no shape is recorded twice;
- every unaffected unit's items are identical to the unvaried run.

The fast tier runs one variant of each kind per source; the exhaustive set is marked slow.
Calendar variants run planning on a varied calendar and require a valid plan.
"""
from __future__ import annotations

import io
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from docket.cli import main
from docket.config import load_workspace
from docket.store.paths import state_root
from tests.conftest import PERSONAS, PINNED_NOW
from tests.fixtures.shapes.variants import Variant, calendar_variants, contract_variants


def _chat(ws: Any) -> list[Variant]:
    src = ws.config.sources["chat"]
    rec = "recordings/chat-gateway.jsonl"
    first_page = lambda space: (lambda r: r.get("args", {}).get("space") == space and "token" not in r["args"]
                                and r["args"].get("after", "").startswith("2026-10-12"))
    msg = contract_variants(sid="chat", contract=src.contract["message"], kind="message", recording=rec,
                            match=first_page("spaces/ingest-war-room"), entries="messages", index=0,
                            unit="spaces/ingest-war-room", mapping=src.mapping)
    space = contract_variants(sid="chat", contract=src.contract["space"], kind="space", recording=rec,
                              match=lambda r: r.get("args", {}).get("tool") == "chat_spaces_list", entries="spaces",
                              index=4, unit="spaces/release-train", mapping={}, unit_kind=True,
                              unit_id_field="name", unit_name_field="displayName")
    return msg + space


def _pager(ws: Any) -> list[Variant]:
    src = ws.config.sources["pager"]
    return contract_variants(sid="pager", contract=src.contract["entry"], kind="entry",
                             recording="recordings/pager-bridge.jsonl",
                             match=lambda r: "position" not in r.get("args", {}), entries="result.items", index=0,
                             unit=None, mapping=src.mapping)


SOURCES = {"chat": ("reference-like", "triage", _chat), "pager": ("support-lead", "shift", _pager)}


def all_variants() -> list[tuple[str, Variant]]:
    out = []
    for sid, (persona, _, gen) in SOURCES.items():
        out += [(sid, v) for v in gen(load_workspace(PERSONAS / persona))]
    return out


def sample() -> list[tuple[str, Variant]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for sid, v in all_variants():
        if (sid, v.kind) not in seen:
            seen.add((sid, v.kind))
            out.append((sid, v))
    return out


def collect(base: Path, sid: str, variant: Variant | None) -> dict[str, Any]:
    persona, workflow, _ = SOURCES[sid]
    ws = base / ("variant" if variant else "baseline")
    shutil.copytree(PERSONAS / persona, ws)
    if variant is not None:
        p = ws / variant.recording
        p.write_text("\n".join(variant.apply(p.read_text().splitlines())) + "\n")
    env = dict(os.environ)
    out = io.StringIO()
    assert main(["run", "start", workflow, "--now", PINNED_NOW, "--workspace", str(ws), "--json"],
                stdout=out, env=env, cwd=str(ws)) == 0
    out = io.StringIO()
    code = main(["collect", "--workspace", str(ws), "--json"], stdout=out, env=env, cwd=str(ws))
    envl = json.loads(out.getvalue())
    run_dir = next((state_root(ws, env) / "runs").iterdir())
    items: dict[str, list[dict[str, Any]]] = {}
    p = run_dir / "gen-0" / "items" / f"{sid}.jsonl"
    for ln in (p.read_text().splitlines() if p.is_file() else []):
        it = json.loads(ln)
        items.setdefault(it.get("unit") or "*", []).append({k: v for k, v in it.items() if k != "record_id"})
    outcomes = json.loads((run_dir / "gen-0" / "outcomes.json").read_text())
    rows = [json.loads(ln) for ln in (run_dir / "unexpected.jsonl").read_text().splitlines()] \
        if (run_dir / "unexpected.jsonl").is_file() else []
    return {"code": code, "env": envl, "items": items, "outcome": outcomes.get(sid, {}),
            "shapes": [(r["area"], r["detail"]) for r in rows]}


_BASE: dict[str, dict[str, Any]] = {}


def baseline(tmp_path_factory: Any, sid: str) -> dict[str, Any]:
    if sid not in _BASE:
        _BASE[sid] = collect(tmp_path_factory.mktemp(f"base-{sid}"), sid, None)
    return _BASE[sid]


def check_variant(tmp_path: Path, tmp_path_factory: Any, sid: str, v: Variant) -> None:
    base = baseline(tmp_path_factory, sid)
    got = collect(tmp_path, sid, v)
    assert got["code"] == 0, (v.name, got["env"].get("error"))  # the run continues
    dup = [s for s, n in Counter(got["shapes"]).items() if n > 1]
    assert dup == [], f"{v.name}: shapes recorded more than once: {dup}"
    new = set(got["shapes"]) - set(base["shapes"])
    assert new <= v.allowed, f"{v.name}: unexpected new shapes {sorted(new - v.allowed)}"
    assert v.required <= set(got["shapes"]), f"{v.name}: missing shape {sorted(v.required)}"
    unit = v.unit or "*"
    units = got["outcome"].get("units") or {}
    state = units.get(unit, {}).get("status") if units else got["outcome"].get("outcome")
    if v.unit_outcome == "skipped":
        assert unit not in got["items"] and unit not in units, v.name
    elif v.unit_outcome == "failed":
        assert state == "failed" and unit not in got["items"], (v.name, state)
    elif v.unit_outcome == "empty":
        assert state in ("empty", "read") and len(got["items"].get(unit, [])) < len(base["items"].get(unit, [])), v.name
    else:
        assert state in ("read", "partial"), (v.name, state)
        if v.kind in ("drop_optional", "unknown_enum", "unknown_field", "nameless", "type_change"):
            assert len(got["items"].get(unit, [])) == len(base["items"].get(unit, [])), v.name  # nothing dropped
        if v.kind == "drop_required":
            assert len(got["items"].get(unit, [])) == len(base["items"].get(unit, [])) - 1, v.name  # only the record
    for u, rows in base["items"].items():
        if u != unit:
            assert got["items"].get(u) == rows, f"{v.name}: unaffected unit {u} changed"


@pytest.mark.parametrize("sid,variant", sample(), ids=lambda x: x.name if isinstance(x, Variant) else x)
def test_variant_takes_the_safe_reading_and_records_one_shape(sid, variant, tmp_path, tmp_path_factory):
    check_variant(tmp_path, tmp_path_factory, sid, variant)


@pytest.mark.slow
@pytest.mark.parametrize("sid,variant", all_variants(), ids=lambda x: x.name if isinstance(x, Variant) else x)
def test_every_variant_exhaustively(sid, variant, tmp_path, tmp_path_factory):
    check_variant(tmp_path, tmp_path_factory, sid, variant)


def test_the_generators_cover_every_variant_kind():
    kinds = {v.kind for _, v in all_variants()}
    assert kinds >= {"drop_optional", "drop_required", "unknown_enum", "type_change", "empty", "nameless",
                     "unknown_field"}
    assert {v.kind for v in calendar_variants("design-review", "sync-platform")} >= {"tombstone", "long_span"}


# --- calendar ------------------------------------------------------------------------------------------


def _planning_rc(ws: Path) -> Any:
    from docket.cli.context import Ctx
    c = Ctx(env=dict(os.environ), cwd=ws, stdin=None, command=None, ns=None)
    c.ns.workspace = str(ws)
    return c, c.run_context()


@pytest.mark.parametrize("variant", calendar_variants("design-review", "sync-platform"), ids=lambda v: v.name)
def test_calendar_variant_still_builds_a_valid_plan(variant, tmp_path):
    from docket.plan.build import run_planning
    from docket.plan.scheduler import validate
    from docket.shapes.unexpected import collecting
    ws = tmp_path / "ws"
    shutil.copytree(PERSONAS / "reference-like", ws)
    io_ = io.StringIO()
    assert main(["run", "start", "week-plan", "--now", PINNED_NOW, "--workspace", str(ws), "--json"],
                stdout=io_, env=dict(os.environ), cwd=str(ws)) == 0
    cal_path = ws / "data" / "calendar.json"
    shapes = {}
    for label, mutate in (("base", None), ("variant", variant.mutate)):
        if mutate is not None:
            cal_path.write_text(json.dumps(mutate(json.loads(cal_path.read_text()))))
        ctx, rc = _planning_rc(ws)
        try:
            with collecting() as col:
                built = run_planning(rc, {"answers": {}, "findings": [], "questions": []})
                shapes[label] = [(s.area, s.detail) for s in col.drain()]
            assert validate(built.result) == [], variant.name  # the plan validates
        finally:
            ctx.close()
    new = set(shapes["variant"]) - set(shapes["base"])
    assert all(any(a == area and d.startswith(pre) for area, pre in variant.allowed) for a, d in new), \
        (variant.name, sorted(new))
    assert variant.required <= set(shapes["variant"]), (variant.name, shapes["variant"])
    assert len(new) <= 1  # one record per distinct shape
