"""Generations, records and the collect artifact (TD §4.3, §4.5).

A generation is one collection pass, kept under ``runs/<id>/gen-<n>/``:

    generation.json          the sources the pass reads and whether it is a refresh
    outcomes.json            each source's outcome and each unit's outcome
    records/<source>.jsonl   the record index: one row per entry, the page body in blobs/
    items/<source>.jsonl     mapped items (or context/<source>.jsonl for context sources)

Generation 0 reads every source of the workflow. A refresh opens generation
n+1 for the volatile sources (or those named), each reading from this run's
proposal (the previous generation's coverage end) without moving the durable
cursor. The collect artifact is a pure function of every generation on disk:
the union of their items, de-duplicated by record identity, later generations
winning, tombstones removing what they name.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from docket.config.digest import digest, pretty_json
from docket.items.mapping import item_id, map_record
from docket.sources.base import ALL_UNITS
from docket.sources.contracts import lookup
from docket.sources.cursors import write_proposal
from docket.store.paths import read_json, write_atomic

if TYPE_CHECKING:
    from docket.engine.context import RunContext
    from docket.sources.collect import SourceResult


def generations(rc: "RunContext") -> list[int]:
    out = []
    for p in rc.run.dir.glob("gen-*/generation.json"):
        try:
            out.append(int(p.parent.name.split("-", 1)[1]))
        except ValueError:
            continue
    return sorted(out)


def current_generation(rc: "RunContext") -> int:
    gens = generations(rc)
    return gens[-1] if gens else 0


def gen_dir(rc: "RunContext", n: int) -> Any:
    return rc.run.dir / f"gen-{n}"


def open_generation(rc: "RunContext", n: int, sources: list[str], *, refresh: bool) -> None:
    write_atomic(gen_dir(rc, n) / "generation.json",
                 pretty_json({"generation": n, "sources": sorted(sources), "refresh": refresh}))
    rc.run.store.db.execute("UPDATE runs SET generation=? WHERE run_id=?", (n, rc.run.id))


def generation_spec(rc: "RunContext", n: int) -> dict[str, Any] | None:
    p = gen_dir(rc, n) / "generation.json"
    return read_json(p) if p.is_file() else None


def map_entry(sid: str, unit: str, view: dict[str, Any], mapping: dict[str, str], fallback: str) -> dict[str, Any]:
    item = map_record(sid, view, mapping, fallback_identity=fallback)
    if "unit" not in mapping:
        item["unit"] = "" if unit == ALL_UNITS else unit
        item["id"] = item_id(sid, item["unit"], item["identity"])
    item["record_id"] = "r_" + digest([sid, item["unit"], item["identity"]])[:16]
    return item


def _outcome_row(res: "SourceResult", cfg: Any) -> dict[str, Any]:
    o = res.outcome
    row: dict[str, Any] = {"kind": res.kind, "model": res.model, "outcome": o.status, "entries": o.entries,
                           "required": cfg.required, "volatile": cfg.volatile, "target": cfg.target}
    for k in ("position", "cause", "reason", "instructions"):
        v = getattr(o, k)
        if v is not None:
            row[k] = v
    for k in ("pages", "stepdowns", "tombstones", "failed_records"):
        if getattr(o, k):
            row[k] = getattr(o, k)
    if res.units and not (len(res.units) == 1 and res.units[0].unit.id == ALL_UNITS):
        row["units"] = {u.unit.id: {**u.outcome.as_dict(), "start": u.cursor.position} for u in res.units}
    return row


def persist(rc: "RunContext", n: int, results: dict[str, "SourceResult"]) -> dict[str, Any]:
    """Write generation ``n``'s records, items, outcomes, proposals and unit registry rows."""
    from docket.sources.units import upsert_units
    ws, r = rc.ws, rc.run
    instant = r.header.clock.instant
    db = r.store.db
    gd = gen_dir(rc, n)
    outcomes: dict[str, Any] = {}
    for sid in sorted(results):
        res = results[sid]
        cfg = ws.config.sources[sid]
        index_rows: list[dict[str, Any]] = []
        mapped: list[dict[str, Any]] = []
        id_path = cfg.mapping.get("id", "$.id")
        for ur in res.units:
            uid = ur.unit.id
            for page in ur.pages:
                blob = r.store.blobs.put(page.body)
                for e in page.entries:
                    row = {"source": sid, "unit": uid, "generation": n, "query": page.request,
                           "interval": {"from": ur.cursor.interval_from, "to": ur.cursor.interval_to},
                           "page": page.number, "entry": e.index, "blob": blob, "received_at": instant,
                           "target": cfg.target}
                    if e.tombstone:
                        ident = lookup(e.raw, id_path)
                        if ident is None:
                            continue
                        u = "" if uid == ALL_UNITS else uid
                        row.update({"record_id": "r_" + digest([sid, u, str(ident)])[:16], "outcome": "tombstone",
                                    "identity": str(ident), "item_id": item_id(sid, u, str(ident))})
                        index_rows.append(row)
                        continue
                    if e.view is None:
                        continue
                    item = map_entry(sid, uid, e.view, cfg.mapping, f"{blob}#{e.index}")
                    row.update({"record_id": item["record_id"], "outcome": "read", "identity": item["identity"],
                                "item_id": item["id"]})
                    index_rows.append(row)
                    mapped.append(item)
        write_atomic(r.paths.records(n, sid), "".join(json.dumps(x, sort_keys=True) + "\n" for x in index_rows))
        sub = "context" if cfg.target == "context" else "items"
        write_atomic(gd / sub / f"{sid}.jsonl", "".join(json.dumps(x, sort_keys=True) + "\n" for x in mapped))
        row = _outcome_row(res, cfg)
        row["index"] = r.paths.records(n, sid).relative_to(r.dir).as_posix()
        outcomes[sid] = row
        with db:
            for ur in res.units:
                pos, cov, complete = ur.proposal
                if res.model == "id-scan":
                    if complete:
                        write_proposal(db, r.id, sid, ur.unit.id, "processed", instant, generation=n,
                                       model="id-scan", complete=True)
                    continue
                write_proposal(db, r.id, sid, ur.unit.id, pos, cov, generation=n, model=res.model, complete=complete)
            if res.discovered:
                upsert_units(db, ws, sid, res.discovered, instant)
    write_atomic(gd / "outcomes.json", pretty_json(outcomes))
    return outcomes


def build_output(rc: "RunContext") -> dict[str, Any]:
    """The collect artifact: every generation's outcomes and the de-duplicated union of their records."""
    sources: dict[str, Any] = {}
    items: dict[str, dict[str, Any]] = {}
    context: dict[str, dict[str, Any]] = {}
    gens = [g for g in generations(rc) if (gen_dir(rc, g) / "outcomes.json").is_file()]
    for g in gens:
        gd = gen_dir(rc, g)
        outs = read_json(gd / "outcomes.json")
        for sid, row in outs.items():
            prev = sources.get(sid, {})
            sources[sid] = {**row, "generations": prev.get("generations", []) + [g]}
            for sub, bucket in (("items", items), ("context", context)):
                p = gd / sub / f"{sid}.jsonl"
                if p.is_file():
                    for ln in p.read_text(encoding="utf-8").splitlines():
                        if ln.strip():
                            it = json.loads(ln)
                            bucket[it["id"]] = it
            idx = gd / "records" / f"{sid}.jsonl"
            if idx.is_file():
                for ln in idx.read_text(encoding="utf-8").splitlines():
                    if '"tombstone"' in ln:
                        row_ = json.loads(ln)
                        if row_.get("outcome") == "tombstone":
                            items.pop(row_.get("item_id"), None)
                            context.pop(row_.get("item_id"), None)
    counts: dict[str, int] = {}
    for it in [*items.values(), *context.values()]:
        counts[it["source"]] = counts.get(it["source"], 0) + 1
    for sid, row in sources.items():
        row["records"] = counts.get(sid, 0)
    return {"generation": gens[-1] if gens else 0, "sources": {k: sources[k] for k in sorted(sources)},
            "items": [items[k] for k in sorted(items)], "context": [context[k] for k in sorted(context)]}


def unavailable_required(output: dict[str, Any] | None) -> dict[str, Any]:
    if not output:
        return {}
    return {sid: s for sid, s in (output.get("sources") or {}).items()
            if s.get("required") and s.get("outcome") == "unavailable"}
