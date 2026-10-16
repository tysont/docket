"""``docket config test`` (CFG-07): the current configuration against a run's raw records.

Mapping, rules, scoring and merge run again from the run's record index and
blobs under the workspace's current configuration, with the run's accepted
assess results as the agent component, and the result is compared with the
run's stored merge artifact. Nothing is written: records, blobs and task results
are only read, and the merge runs in memory. Dispositions and task-store lines are
the ones the stored merge recorded, so only configuration differs between the two.
"""
from __future__ import annotations

import json
from typing import Any

from docket.shapes.unexpected import collecting


def _rows(path: Any) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def rebuild_items(ws: Any, store: Any, run: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-map every generation's records under the current source configuration.
    Later generations win; tombstones remove what they name (TD §4.5)."""
    from docket.items.mapping import item_id
    from docket.sources.base import ALL_UNITS
    from docket.sources.contracts import ContractChecker, lookup
    from docket.sources.generations import map_entry
    notes: list[str] = []
    gens = sorted(int(p.parent.name.split("-", 1)[1]) for p in run.dir.glob("gen-*/generation.json")
                  if p.parent.name.split("-", 1)[1].isdigit())
    items: dict[str, dict[str, Any]] = {}
    bodies: dict[str, Any] = {}
    missing: set[str] = set()
    with collecting():  # shapes found again are already recorded on the run; this pass reports none
        for g in gens:
            for idx in sorted((run.dir / f"gen-{g}" / "records").glob("*.jsonl")):
                sid = idx.stem
                cfg = ws.config.sources.get(sid)
                if cfg is None:
                    if sid not in missing:
                        missing.add(sid)
                        notes.append(f"source {sid} is no longer configured; its records were skipped")
                    continue
                if cfg.target == "context":
                    continue
                checker = ContractChecker(sid, cfg.contract, extend_base=f"sources.{sid}.contract")
                for row in _rows(idx):
                    blob = row.get("blob")
                    if blob not in bodies:
                        try:
                            bodies[blob] = json.loads(store.blobs.get(blob).decode("utf-8"))
                        except (OSError, ValueError, UnicodeDecodeError):
                            bodies[blob] = None
                    entries = lookup(bodies[blob], cfg.paging.entries) if bodies[blob] is not None else None
                    entry = row.get("entry")
                    if not isinstance(entries, list) or not isinstance(entry, int) or not 0 <= entry < len(entries):
                        continue
                    raw = entries[entry]
                    unit = row.get("unit") or ALL_UNITS
                    if row.get("outcome") == "tombstone":
                        ident = lookup(raw, cfg.mapping.get("id", "$.id"))
                        if ident is not None:
                            u = "" if unit == ALL_UNITS else unit
                            items.pop(item_id(sid, u, str(ident)), None)
                        continue
                    checked = checker.check(cfg.paging.kind, raw)
                    if checked.view is None:
                        continue
                    it = map_entry(sid, unit, checked.view, cfg.mapping, f"{blob}#{entry}")
                    items[it["id"]] = it
    return [items[k] for k in sorted(items)], notes


def stored_agent_rows(run: Any) -> dict[str, dict[str, Any]]:
    """Accepted assess rows of the run, by item id (from each task's ``result.json``)."""
    out: dict[str, dict[str, Any]] = {}
    tdir = run.dir / "tasks"
    if not tdir.is_dir():
        return out
    for d in sorted(tdir.iterdir()):
        pack, res = d / "pack.json", d / "result.json"
        if not (pack.is_file() and res.is_file()):
            continue
        try:
            if json.loads(pack.read_text(encoding="utf-8")).get("kind") != "assess":
                continue
            rows = json.loads(res.read_text(encoding="utf-8")).get("items") or []
        except ValueError:
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("id"):
                out[str(r["id"])] = {"keep": bool(r.get("keep")), "score": int(r.get("score") or 0),
                                     **({"topic": r["topic"]} if r.get("topic") else {})}
    return out


def stored_merge(run: Any) -> tuple[str | None, dict[str, Any] | None]:
    latest = run.paths.artifacts("merge") / "latest"
    if not latest.is_file():
        return None, None
    dg = latest.read_text().strip()
    p = run.paths.artifact("merge", dg)
    if not p.is_file():
        return dg, None
    return dg, json.loads(p.read_text(encoding="utf-8")).get("data")


def _index(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Every item id (representatives and members) -> its topic's row."""
    out: dict[str, dict[str, Any]] = {}
    for it in (data or {}).get("items") or []:
        out[it["id"]] = it
        for mem in it.get("members") or []:
            out.setdefault(mem["id"], it)
    return out


def questions(data: dict[str, Any] | None, budget: int | None) -> list[str]:
    """The items decide would ask, by its own selection: kept, at or above the floor, by score."""
    if not data:
        return []
    floor = int((data.get("scoring") or {}).get("floor") or 0)
    kept = [it for it in data.get("items") or [] if it.get("keep") and int(it.get("score") or 0) >= floor]
    kept.sort(key=lambda it: (-int(it.get("score") or 0), it["id"]))
    return [it["id"] for it in (kept[:budget] if budget is not None else kept)]


def _brief(it: dict[str, Any]) -> dict[str, Any]:
    return {"id": it["id"], "title": (it.get("title") or "")[:100], "source": it.get("source"),
            "score": it.get("score"), "status": it.get("status"), "reason": it.get("reason")}


def compare(old: dict[str, Any] | None, new: dict[str, Any], budget: int | None) -> dict[str, Any]:
    oi, ni = _index(old), _index(new)
    old_top = {it["id"]: it for it in (old or {}).get("items") or []}
    new_top = {it["id"]: it for it in new.get("items") or []}
    kept_before = {k for k, v in old_top.items() if v.get("keep")}
    kept_after = {k for k, v in new_top.items() if v.get("keep")}
    kept_added = [_brief(new_top[k]) for k in sorted(kept_after - kept_before)]
    kept_removed = []
    for k in sorted(kept_before - kept_after):
        now = ni.get(k)
        kept_removed.append({**_brief(old_top[k]), "now": _brief(now) if now else None})
    score_changes, route_changes, status_changes = [], [], []
    for k in sorted(set(old_top) & set(new_top)):
        a, b = old_top[k], new_top[k]
        if a.get("score") != b.get("score"):
            score_changes.append({"id": k, "title": (b.get("title") or "")[:100], "before": a.get("score"),
                                  "after": b.get("score"), "components_before": a.get("components"),
                                  "components_after": b.get("components")})
        ra, rb = (a.get("signals") or {}).get("route"), (b.get("signals") or {}).get("route")
        if ra != rb:
            route_changes.append({"id": k, "title": (b.get("title") or "")[:100], "before": ra, "after": rb})
        if a.get("status") != b.get("status"):
            status_changes.append({"id": k, "title": (b.get("title") or "")[:100], "before": a.get("status"),
                                   "after": b.get("status"), "reason": b.get("reason")})
    qa, qb = questions(old, budget), questions(new, budget)
    return {
        "items": {"before": len(oi), "after": len(ni)},
        "topics": {"before": len(old_top), "after": len(new_top)},
        "kept": {"before": len(kept_before), "after": len(kept_after)},
        "kept_added": kept_added, "kept_removed": kept_removed,
        "score_changes": score_changes, "route_changes": route_changes, "status_changes": status_changes,
        "questions": {"budget": budget, "added": [x for x in qb if x not in qa], "removed": [x for x in qa if x not in qb]},
        "new_items": sorted(set(ni) - set(oi)), "gone_items": sorted(set(oi) - set(ni)),
        "agent_pending": [it["id"] for it in new.get("items") or [] if it.get("agent_pending")],
        "changed": bool(kept_added or kept_removed or score_changes or route_changes or status_changes
                        or set(ni) ^ set(oi) or qa != qb),
    }


def config_test(ws: Any, store: Any, run: Any) -> dict[str, Any]:
    from docket.signals.merge import build_merge
    from docket.signals.stage import inputs_for
    items, notes = rebuild_items(ws, store, run)
    col_latest = run.paths.artifacts("collect") / "latest"
    sources: dict[str, Any] = {}
    if col_latest.is_file():
        p = run.paths.artifact("collect", col_latest.read_text().strip())
        if p.is_file():
            sources = (json.loads(p.read_text(encoding="utf-8")).get("data") or {}).get("sources") or {}
    dg, old = stored_merge(run)
    with collecting() as col:
        inp = inputs_for(ws, store, run, items, stored_agent_rows(run), sources,
                         suppression=(old or {}).get("suppression_inputs"))
        new = build_merge(inp)
        shapes = [s.as_dict() for s in col.drain()]
    wf = ws.config.workflows.get(run.header.workflow)
    budget = wf.questions.budget if wf is not None else None
    diff = compare(old, new, budget)
    return {"run": run.id, "stored_merge": dg, "compared": old is not None, "records_items": len(items),
            "notes": notes, "unexpected": shapes, "counts": new["counts"], "diff": diff, "written": False}
