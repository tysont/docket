"""What a read node keeps (TD §4.3): the records of one source, as read in this run.

Each source read in a run has its own folder, ``runs/<id>/read/<source>/``:

    outcome.json     the source's outcome and each unit's outcome
    records.jsonl    the record index: one row per entry, the page body in blobs/
    items.jsonl      mapped items (context.jsonl for a context source)

Sources are read by separate nodes, often at the same time, so nothing here is shared
between sources. A node's output is a pure function of its folder: the items, with what a
tombstone names removed.
"""
from __future__ import annotations

import json
from pathlib import Path
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


def source_dir(run_dir: Path, sid: str) -> Path:
    return run_dir / "read" / sid


def index_paths(run_dir: Path) -> list[Path]:
    """Every record index of a run."""
    return sorted(run_dir.glob("read/*/records.jsonl"))


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
    for k in ("pages", "stepdowns", "tombstones", "failed_records", "retries"):
        if getattr(o, k):
            row[k] = getattr(o, k)
    if res.quiet:
        row["quiet_units"] = len(res.quiet)  # not read: no activity since their cursor (TD §6.9)
    if res.units and not (len(res.units) == 1 and res.units[0].unit.id == ALL_UNITS):
        row["units"] = {u.unit.id: {**u.outcome.as_dict(), "start": u.cursor.position} for u in res.units}
    return row


def persist(rc: "RunContext", sid: str, res: "SourceResult") -> dict[str, Any]:
    """Write one source's records, items, outcome, cursor proposals and unit registry rows."""
    from docket.sources.units import upsert_units
    ws, r = rc.ws, rc.run
    instant = r.header.clock.instant
    db = r.store.db
    sd = source_dir(r.dir, sid)
    cfg = ws.config.sources[sid]
    index_rows: list[dict[str, Any]] = []
    mapped: list[dict[str, Any]] = []
    id_path = cfg.mapping.get("id", "$.id")
    for ur in res.units:
        uid = ur.unit.id
        for page in ur.pages:
            blob = r.store.blobs.put(page.body)
            for e in page.entries:
                row = {"source": sid, "unit": uid, "query": page.request,
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
    write_atomic(sd / "records.jsonl", "".join(json.dumps(x, sort_keys=True) + "\n" for x in index_rows))
    sub = "context" if cfg.target == "context" else "items"
    write_atomic(sd / f"{sub}.jsonl", "".join(json.dumps(x, sort_keys=True) + "\n" for x in mapped))
    row = _outcome_row(res, cfg)
    row["index"] = (sd / "records.jsonl").relative_to(r.dir).as_posix()
    with db:
        for ur in res.units:
            pos, cov, complete = ur.proposal
            if res.model == "id-scan":
                if complete:
                    write_proposal(db, r.id, sid, ur.unit.id, "processed", instant, model="id-scan", complete=True)
                continue
            write_proposal(db, r.id, sid, ur.unit.id, pos, cov, model=res.model, complete=complete)
        if res.discovered:
            from docket.sources.units import membership_row
            member = membership_row(upsert_units(db, ws, sid, res.discovered, instant))
            counts: dict[str, int] = {}
            for x in mapped:
                counts[x.get("unit") or ""] = counts.get(x.get("unit") or "", 0) + 1
            loose = []
            for uid in sorted(counts, key=lambda k: -counts[k]):
                u = db.execute("SELECT name, class FROM units WHERE source=? AND unit_id=?", (sid, uid)).fetchone()
                if u is not None and u["class"] is None:
                    loose.append({"unit": uid, "name": u["name"], "records": counts[uid]})
            if loose:
                member["unclassified_active"] = loose[:50]
                member["unclassified_active_count"] = len(loose)
            row["membership"] = member
    write_atomic(sd / "outcome.json", pretty_json(row))
    return row


def output(rc: "RunContext", sid: str) -> dict[str, Any]:
    """A read node's output: the source's outcome and its items, tombstoned ones removed."""
    sd = source_dir(rc.run.dir, sid)
    row = read_json(sd / "outcome.json")
    buckets: dict[str, dict[str, dict[str, Any]]] = {"items": {}, "context": {}}
    for sub, bucket in buckets.items():
        p = sd / f"{sub}.jsonl"
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    it = json.loads(ln)
                    bucket[it["id"]] = it
    idx = sd / "records.jsonl"
    if idx.is_file():
        for ln in idx.read_text(encoding="utf-8").splitlines():
            if '"tombstone"' in ln:
                rec = json.loads(ln)
                if rec.get("outcome") == "tombstone":
                    buckets["items"].pop(rec.get("item_id"), None)
                    buckets["context"].pop(rec.get("item_id"), None)
    row["records"] = len(buckets["items"]) + len(buckets["context"])
    return {"sources": {sid: row}, "items": [buckets["items"][k] for k in sorted(buckets["items"])],
            "context": [buckets["context"][k] for k in sorted(buckets["context"])]}


def unavailable(output_: dict[str, Any] | None) -> dict[str, Any]:
    """The required sources of an output that could not be read."""
    if not output_:
        return {}
    return {sid: s for sid, s in (output_.get("sources") or {}).items()
            if s.get("required") and s.get("outcome") == "unavailable"}
