"""The apply boundary (TD §5.4): a run never goes back across a write.

When an apply or write node starts writing, it pins the digest of every node upstream of
it (``run.json`` ``applied``). From then on those nodes are read from their pinned
artifacts and never recomputed: a later change upstream does not stale the plan, and the
node's own writes to a task store, an agenda or a calendar that an upstream node read
cannot push the run backward either.

An upstream change is still noticed. A pinned node whose configuration digest or code
schema version differs from its artifact has *drifted*; ``docket next`` then returns
``wait`` with reason ``post_apply_change``, the commands to inspect it, and
``docket run continue`` to finish the run on what was written (the change applies to the
next run).
"""
from __future__ import annotations

from typing import Any

from docket.config.digest import digest
from docket.store.paths import read_json


def crossed(run: Any) -> bool:
    return bool(run.header.applied)


def pins(run: Any) -> dict[str, str]:
    """node -> pinned digest, over every node that has started writing."""
    out: dict[str, str] = {}
    for row in run.header.applied.values():
        out.update(row.get("pinned") or {})
    return out


def pin(rc: Any, node: str, plan_digest: str) -> None:
    """Record the boundary before ``node`` writes its first operation."""
    hdr = rc.run.header
    have = hdr.applied.get(node)
    if have and have.get("plan") == plan_digest and have.get("pinned"):
        return
    upstream = rc.ancestors(node)
    hdr.applied[node] = {"plan": plan_digest,
                         "pinned": {ev.name: ev.digest for ev in rc.evaluate() if ev.name in upstream and ev.digest}}
    rc.run.save_header()


def pinned_eval(rc: Any, impl: Any, dg: str) -> Any:
    """The node as its pinned artifact recorded it, or None when that artifact is missing."""
    from docket.engine.nodes import Eval, InputRef
    path = rc.run.paths.artifact(impl.name, dg)
    if not path.is_file():
        return None
    art = read_json(path)
    ev = Eval(impl.name, impl.verb, "current", dg, None, art["data"], digest(art["data"]), art.get("config_digest"),
              [InputRef.model_validate(i) for i in art.get("inputs") or []], how=impl.how)
    why = []
    if art.get("config_digest") is not None and art.get("config_digest") != impl.current_config_digest(rc):
        why.append("configuration changed")
    if art.get("schema") is not None and art.get("schema") != impl.schema_version:
        why.append("code schema changed")
    ev.extra = {"pinned": True, "drift": why}
    return ev


def drift(evals: list[Any]) -> list[dict[str, Any]]:
    return [{"node": ev.name, "why": ", ".join(ev.extra["drift"])} for ev in evals
            if ev.extra.get("pinned") and ev.extra.get("drift")]


def drift_digest(rows: list[dict[str, Any]]) -> str:
    return digest(rows)


def unacknowledged(rc: Any) -> list[dict[str, Any]]:
    rows = drift(rc.evaluate())
    if not rows or rc.run.header.drift_ack == drift_digest(rows):
        return []
    return rows
