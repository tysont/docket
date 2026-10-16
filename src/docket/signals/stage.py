"""Signals in the engine: assess item selection and the merge stage's inputs (TD §5.1, §7.3).

Unit classifications come from the unit registry and are part of the merge
stage digest (``merge_extra``). Dispositions and the task store's snapshot are
read when merge runs and recorded in the artifact: a later write by apply or
close never stales merge, so a run is never pushed back across apply (TD §5.4).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docket.config.digest import digest
from docket.signals.engine import SignalEnv, env_for, evaluate, needs_agent
from docket.signals.merge import MergeInputs, build_merge

if TYPE_CHECKING:
    from docket.engine.context import RunContext


def units_view(db: sqlite3.Connection, sources: set[str] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in db.execute("SELECT source, unit_id, name, kind, class, tier FROM units ORDER BY source, unit_id"):
        if sources is None or r["source"] in sources:
            out[(r["source"], r["unit_id"])] = {"name": r["name"], "kind": r["kind"], "class": r["class"],
                                                "tier": r["tier"]}
    return out


def _env(rc: "RunContext", sources: set[str] | None = None) -> SignalEnv:
    clock = rc.run.header.clock
    return env_for(rc.ws, units=units_view(rc.run.store.db, sources), now=clock.instant, run_date=clock.date)


@dataclass
class Selection:
    candidates: list[dict[str, Any]]
    must_surface: set[str]
    skipped: dict[str, str]       # item id -> rules_only | dismissed


def assess_selection(rc: "RunContext", items: list[dict[str, Any]]) -> Selection:
    """Items that need an assess task: not ``assess: rules_only`` and not dismissed.
    Must-surface items are always candidates unless rules alone assess them."""
    ev = evaluate(items, _env(rc, {it["source"] for it in items}))
    by_id = {it["id"]: it for it in items}
    cands, must, skipped = [], set(), {}
    for it in ev:
        if it["signals"]["surface"]:
            must.add(it["id"])
        if needs_agent(it):
            cands.append(by_id[it["id"]])
        else:
            skipped[it["id"]] = "rules_only" if it["signals"]["assess"] == "rules_only" else "dismissed"
    return Selection(cands, must, skipped)


def merge_extra(rc: "RunContext") -> dict[str, Any]:
    return {"units": digest(sorted(([k[0], k[1], v] for k, v in units_view(rc.run.store.db).items()),
                                   key=lambda r: (r[0], r[1])))}


def dispositions(store: Any) -> list[dict[str, Any]]:
    rows = [dict(r) for r in store.db.execute(
        "SELECT topic_id, decision, run_id, item, until, note FROM dispositions ORDER BY topic_id")]
    instants: dict[str, str | None] = {}
    for r in rows:
        rid = r.get("run_id")
        if rid and rid not in instants:
            hp = store.runs_dir / rid / "run.json"
            try:
                instants[rid] = json.loads(hp.read_text(encoding="utf-8"))["clock"]["instant"]
            except (OSError, ValueError, KeyError):
                row = store.db.execute("SELECT date FROM runs WHERE run_id=?", (rid,)).fetchone()
                instants[rid] = f"{row['date']}T23:59:59+00:00" if row else None
        r["recorded_at"] = instants.get(rid) if rid else None
    return rows


def task_stores(ws: Any, run: Any) -> list[dict[str, Any]]:
    """Open task lines of every ``markdown_tasks`` destination, read without writing (a sandbox
    run reads its twin when one exists)."""
    from docket.text.grammar import Grammar
    out = []
    for did in sorted(ws.config.destinations):
        cfg = ws.config.destinations[did]
        if cfg.adapter != "markdown_tasks":
            continue
        rel = cfg.path or f"{did}.md"
        path: Path = ws.root / rel
        if run is not None and run.header.sandbox and (run.paths.sandbox / did / rel).is_file():
            path = run.paths.sandbox / did / rel
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        g = Grammar.from_config(cfg.grammar)
        lines = []
        for ln in text.splitlines():
            t = g.parse(ln)
            if t is not None and t.status == "open":
                lines.append(ln)
        out.append({"destination": did, "path": rel, "open": lines, "digest": digest(text)})
    return out


def inputs_for(ws: Any, store: Any, run: Any, items: list[dict[str, Any]], agent: dict[str, dict[str, Any]],
               sources: dict[str, Any], suppression: dict[str, Any] | None = None) -> MergeInputs:
    """Merge inputs for a run. ``suppression`` replays the dispositions and task-store lines a stored
    merge artifact recorded, instead of reading the current ones."""
    clock = run.header.clock
    env = env_for(ws, units=units_view(store.db, {it["source"] for it in items}), now=clock.instant,
                  run_date=clock.date)
    sup = suppression or {}
    disp = sup["dispositions"] if isinstance(sup.get("dispositions"), list) else dispositions(store)
    stores = sup["task_stores"] if isinstance(sup.get("task_stores"), list) else task_stores(ws, run)
    return MergeInputs(items=items, env=env, agent=agent, dispositions=disp, task_stores=stores, sources=sources,
                       run_date=clock.date, run_id=run.id)


def agent_rows(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Accepted assess rows as the assess stage's output carries them (``keep``, ``score``,
    and ``assess.topic`` when the task kind supplies one)."""
    out = {}
    for it in items:
        if "keep" in it or "score" in it:
            row = {"keep": bool(it.get("keep")), "score": int(it.get("score") or 0)}
            topic = (it.get("assess") or {}).get("topic") if isinstance(it.get("assess"), dict) else None
            if topic:
                row["topic"] = topic
            out[it["id"]] = row
    return out


def execute_merge(rc: "RunContext", up: Any) -> dict[str, Any]:
    items = list(up.output["items"])
    col = rc.eval_or_none("collect")
    sources = (col.output or {}).get("sources", {}) if col is not None else {}
    inp = inputs_for(rc.ws, rc.run.store, rc.run, items, agent_rows(items), sources)
    data = build_merge(inp)
    data["suppression_inputs"] = {"dispositions": inp.dispositions, "task_stores": inp.task_stores}
    return data
