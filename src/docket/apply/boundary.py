"""The apply boundary (TD §5.4): a run never goes back across apply.

When apply starts writing, it pins the digest of every stage before it
(``run.json`` ``pinned``). From then on those stages are read from their pinned
artifacts and never recomputed: a later change upstream does not stale the
plan, and apply's own writes to a task store or calendar the plan read cannot
push the run backward either.

An upstream change is still noticed. A pinned stage whose configuration
section digest or code schema version differs from its artifact has *drifted*;
``docket next`` then returns ``wait`` with reason ``post_apply_change``, the
commands to inspect it, and ``docket run continue`` to finish the run on the
approved plan (the change applies to the next run).
"""
from __future__ import annotations

from typing import Any

from docket.config.digest import digest
from docket.store.paths import read_json


def crossed(run: Any) -> bool:
    return bool(run.header.applied_plan)


def pin(rc: Any, plan_digest: str) -> None:
    """Record the boundary before the first operation is written."""
    hdr = rc.run.header
    if hdr.applied_plan == plan_digest and hdr.pinned:
        return
    pins = {}
    for ev in rc.evaluate():
        if ev.name in ("apply", "close"):
            break
        pins[ev.name] = ev.digest
    hdr.applied_plan = plan_digest
    hdr.pinned = pins
    rc.run.save_header()


def pinned_eval(rc: Any, impl: Any) -> Any:
    """The stage as its pinned artifact recorded it, or None when no pinned artifact exists."""
    from docket.engine.stages import InputRef, StageEval
    run = rc.run
    dg = run.header.pinned.get(impl.name)
    if dg is None:
        p = run.paths.artifacts(impl.name) / "latest"
        dg = p.read_text().strip() if p.is_file() else None
    if dg is None:
        return None
    path = run.paths.artifact(impl.name, dg)
    if not path.is_file():
        return None
    art = read_json(path)
    ev = StageEval(impl.name, impl.kind, "current", dg, None, art["data"], digest(art["data"]), art.get("config_digest"),
                   [InputRef.model_validate(i) for i in art.get("inputs") or []])
    why = []
    if art.get("config_digest") is not None and art.get("config_digest") != rc.partial_config(impl.config_sections):
        why.append("configuration changed")
    if art.get("schema") is not None and art.get("schema") != impl.schema_version:
        why.append("code schema changed")
    ev.extra = {"pinned": True, "drift": why}
    return ev


def drift(evals: list[Any]) -> list[dict[str, Any]]:
    return [{"stage": ev.name, "why": ", ".join(ev.extra["drift"])} for ev in evals
            if ev.extra.get("pinned") and ev.extra.get("drift")]


def drift_digest(rows: list[dict[str, Any]]) -> str:
    return digest(rows)


def unacknowledged(rc: Any) -> list[dict[str, Any]]:
    rows = drift(rc.evaluate())
    if not rows or rc.run.header.drift_ack == drift_digest(rows):
        return []
    return rows
