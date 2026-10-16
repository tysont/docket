"""Accepted results, indexed by task input digest (TD §9.4, §9.6).

An accepted result is written to the run's ``tasks/<task-id>/result.json`` and
to the workspace database's ``task_results`` table under the pack's input
digest. A pack whose digest has an accepted result, in this run or an earlier
one, needs no new task: a pack digest covers the kind's instructions and output
model, the items as the task saw them, the profile excerpts and the vocabulary.

Review goes further: an item whose review view (its evidence and assessment) is
unchanged since a review accepted in an earlier generation keeps that verdict
without a new task, so only new or changed items enter the next review pack.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from docket.config.digest import pretty_json
from docket.store.paths import read_json, write_atomic

if TYPE_CHECKING:
    from docket.engine.context import RunContext
    from docket.tasks.kinds import TaskKind


def accepted(rc: "RunContext", packs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """task id -> accepted result for every pack that has one (this run first, then the index)."""
    out: dict[str, dict[str, Any]] = {}
    missing: dict[str, str] = {}
    for p in packs:
        rp = rc.run.paths.task(p["task_id"]) / "result.json"
        if rp.is_file():
            out[p["task_id"]] = read_json(rp)
        else:
            missing[p["input_digest"]] = p["task_id"]
    if missing:
        try:
            q = ",".join("?" for _ in missing)
            rows = rc.run.store.db.execute(
                f"SELECT input_digest, result FROM task_results WHERE input_digest IN ({q})", sorted(missing)).fetchall()
        except Exception:  # an index read failure only costs a re-issued task
            rows = []
        for dg, body in rows:
            out[missing[dg]] = json.loads(body)
    return out


def record(rc: "RunContext", pack: dict[str, Any], body: dict[str, Any]) -> None:
    """Write an accepted result to the run and the index."""
    write_atomic(rc.run.paths.task(pack["task_id"]) / "result.json", pretty_json(body))
    stamp = datetime.now(timezone.utc).isoformat()
    db = rc.run.store.db
    with db:
        db.execute("INSERT OR REPLACE INTO task_results (input_digest, kind, task_id, run_id, result, accepted_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (pack["input_digest"], pack["kind"], pack["task_id"], rc.run.id,
                    json.dumps(body, sort_keys=True, ensure_ascii=False), stamp))


def carried_rows(rc: "RunContext", kind: "TaskKind", generation: int) -> dict[str, dict[str, Any]]:
    """carry key -> accepted row, from this run's tasks of ``kind`` issued before ``generation``."""
    from docket.tasks.packs import carry_key
    out: dict[str, dict[str, Any]] = {}
    tasks_dir = rc.run.dir / "tasks"
    if not tasks_dir.is_dir():
        return out
    for tdir in sorted(tasks_dir.iterdir()):
        pp, rp = tdir / "pack.json", tdir / "result.json"
        if not (pp.is_file() and rp.is_file()):
            continue
        try:
            pack = read_json(pp)
        except ValueError:
            continue
        if pack.get("kind") != kind.name or pack.get("spec") != kind.spec_digest:
            continue
        if int(pack.get("issued_generation", generation)) >= generation:
            continue
        rows = {r.get("id"): r for r in read_json(rp).get("items", [])}
        for view in pack.get("items", []):
            row = rows.get(view.get("id"))
            if row is not None:
                out[carry_key(kind, view)] = row
    return out
