"""Source, unit and ledger commands: ``source list``, ``source test <id> [--unit] [--limit]``,
``units list|preview|set|unset`` and ``ledger list|show|import|reconcile``. A source is read
in a run by its read node (``docket do <node>``)."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import ExternalError, InputError
from docket.cli.registry import Arg, command


@command("source list", "List configured sources: transport, cursor model, capabilities and the graphs that read "
                        "them.",
         examples=["docket source list --json"])
def cmd_source_list(ctx: Any, ns: Any) -> Result:
    ws = ctx.workspace()
    rows = []
    for sid in sorted(ws.config.sources):
        s = ws.config.sources[sid]
        kind = ws.config.transports[s.transport].kind if s.transport else "files"
        caps = set(s.capabilities or ["read"])
        if s.cursor.effective == "units":
            caps.add("discover")
        if s.resolve:
            caps.add("resolve")
        if s.target == "context":
            caps.add("context")
        rows.append({"id": sid, "kind": kind, "transport": s.transport, "extends": s.extends,
                     "cursor": s.cursor.model, "capabilities": sorted(caps), "target": s.target,
                     "volatile": s.volatile, "required": s.required, "description": s.description,
                     "graphs": sorted(g for g, nodes in ws.config.graphs.items()
                                      if any(n.read == sid for n in nodes.values()))})
    text = "\n".join(f"{r['id']:<16} {r['kind']:<11} {r['cursor']:<12} {','.join(r['capabilities'])}" for r in rows)
    return Result({"sources": rows}, text or "no sources")


@command("source test", "Read a small sample from one source and show the mapped items; writes nothing durable.",
         args=[Arg("id", "Source id."), Arg("--unit", "Read only this unit."),
               Arg("--limit", "Entries to read (default 5).", default="5"),
               Arg("--now", "Read as of this ISO instant instead of the wall clock (recorded transports, tests).")],
         effects=["contacts the source's system; writes no record, cursor, unit row or run file"],
         examples=["docket source test chat --limit 3 --json", "docket source test chat --unit spaces/room-1 --json"])
def cmd_source_test(ctx: Any, ns: Any) -> Result:
    import asyncio

    from docket.config.loader import closest
    from docket.sources.reader import Starts, read_sources
    from docket.sources.records import map_entry
    from docket.transports.base import Secrets
    ws = ctx.workspace()
    if ns.id not in ws.config.sources:
        raise InputError("input.unknown_source", f"No source {ns.id!r}.", hint="docket source list",
                         details={"suggestion": closest(ns.id, list(ws.config.sources)),
                                  "sources": sorted(ws.config.sources)})
    try:
        limit = max(1, int(ns.limit))
    except ValueError:
        raise InputError("input.bad_argument", f"--limit must be a number, not {ns.limit!r}") from None
    from docket.engine.clock import pin, run_date
    clock = pin(ws.config.profile.timezone, now=ns.now)
    cfg = ws.config.sources[ns.id]
    durable: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    store = ctx.store()
    if (store.root / "docket.db").is_file():  # never create state: a sample reads from the source's start
        from docket.sources.cursors import durable as durable_rows
        durable = durable_rows(store.db, [ns.id])
    res = asyncio.run(read_sources(ws, [ns.id], env=dict(ctx.env), instant=clock.instant, run_date=run_date(clock),
                                   starts=Starts(durable=durable), log_dir=None, secrets=Secrets(),
                                   entry_limit=limit, only_unit=ns.unit))[ns.id]
    if cfg.cursor.effective == "units" and ns.unit and not res.units and res.outcome.status not in (
            "failed", "unavailable", "skipped"):
        raise InputError("input.unknown_unit", f"Source {ns.id!r} discovered no unit {ns.unit!r}.",
                         hint=f"docket source test {ns.id}",
                         details={"units": [u.id for u in res.discovered][:50]})
    if res.outcome.status in ("failed", "unavailable") and not any(u.pages for u in res.units):
        cause = res.outcome.cause or {}
        code = cause.get("code") if cause.get("code") in _EXTERNAL else (
            "source.auth_failed" if res.outcome.status == "unavailable" and cfg.auth else "source.unavailable")
        raise ExternalError(code, res.outcome.reason or cause.get("message") or res.outcome.status,
                            hint=res.outcome.instructions or f"docket source test {ns.id}",
                            details={"outcome": res.outcome.as_dict()})
    items, units = [], []
    for ur in res.units:
        units.append({"unit": ur.unit.id, "name": ur.unit.name, "outcome": ur.outcome.as_dict()})
        for page in ur.pages:
            for e in page.entries:
                if e.view is not None and len(items) < limit:
                    items.append(map_entry(ns.id, ur.unit.id, e.view, cfg.mapping, f"sample#{page.number}.{e.index}"))
    data = {"source": ns.id, "outcome": res.outcome.as_dict(), "units": units,
            "discovered": [{"id": u.id, "name": u.name, "type": u.type, "members": u.members} for u in res.discovered],
            "quiet_units": len(res.quiet), "items": items, "written": False}
    text = [f"{ns.id}: {res.outcome.status} ({len(items)} item(s) shown)"]
    if res.quiet:
        text.append(f"  {len(res.quiet)} unit(s) not read: no activity since their cursor")
    text += [f"  [{it['unit'] or '-'}] {it['title']}" for it in items]
    return Result(data, "\n".join(text))


_EXTERNAL = {"source.failed", "source.auth_failed", "source.contract.type_changed", "source.contract.not_json",
             "source.budget_exhausted", "source.truncated", "transport.timeout", "transport.too_large",
             "transport.status", "transport.protocol", "transport.unavailable", "transport.no_recording",
             "transport.error"}


@command("units list", "List the unit registry: discovered units and their classification. Units a discovery "
         "no longer lists are left out unless --gone is given.",
         args=[Arg("--source", "Only units of this source."),
               Arg("--gone", "Include units that are gone, with when.", action="store_true"),
               Arg("--unclassified", "Only units with no class.", action="store_true")],
         examples=["docket units list --json", "docket units list --unclassified --json"])
def cmd_units_list(ctx: Any, ns: Any) -> Result:
    ctx.workspace_root()
    db = ctx.store().db
    where, params = [], []
    if ns.source:
        where.append("source=?")
        params.append(ns.source)
    if not ns.gone:
        where.append("gone_at IS NULL")
    if ns.unclassified:
        where.append("class IS NULL")
    rows = db.execute("SELECT * FROM units" + (" WHERE " + " AND ".join(where) if where else "")
                      + " ORDER BY source, unit_id", params).fetchall()
    out = [dict(r) for r in rows]
    text = "\n".join(f"{r['source']:<12} {r['unit_id']:<28} {r['kind'] or '-':<10} {r['class'] or '-':<12} "
                     f"{r['classified_by'] or 'unclassified'}" for r in out)
    return Result({"units": out}, text or "no units")


@command("units preview", "Apply the current unit rules to the registry and show the result; writes nothing. "
         "Counts per class and tier, every unit whose classification would change, and the units no rule matches.",
         args=[Arg("--source", "Only units of this source."),
               Arg("--class", "List every unit the rules put in this class ('none' for units no rule matches)."),
               Arg("--gone", "Include units that are gone.", action="store_true")],
         effects=["reads the unit registry; contacts no source and writes nothing"],
         examples=["docket units preview --json", "docket units preview --class none --json"])
def cmd_units_preview(ctx: Any, ns: Any) -> Result:
    from collections import Counter

    from docket.sources.units import preview
    ws = ctx.workspace()
    rows = preview(ctx.store().db, ws, ns.source, gone=ns.gone)
    classes = Counter(r["now"]["class"] or "none" for r in rows)
    tiers = Counter(r["now"]["tier"] or "none" for r in rows)
    changed = [{"unit": r["unit"], "source": r["source"], "name": r["name"], "was": r["was"], "now": r["now"]}
               for r in rows if (r["was"]["kind"], r["was"]["class"], r["was"]["tier"]) !=
               (r["now"]["kind"], r["now"]["class"], r["now"]["tier"])]
    data: dict[str, Any] = {"units": len(rows), "classes": dict(sorted(classes.items())),
                            "tiers": dict(sorted(tiers.items())), "changed_count": len(changed),
                            "changed": changed[:200], "written": False}
    text = [f"{len(rows)} unit(s); {len(changed)} would change"]
    text += [f"  {k:<18} {v}" for k, v in sorted(classes.items(), key=lambda kv: -kv[1])]
    unit_class = getattr(ns, "class", None)
    if unit_class:
        want = None if unit_class == "none" else unit_class
        listed = [{"unit": r["unit"], "source": r["source"], "name": r["name"], "type": r["type"],
                   "members": r["members"], "first_seen": r["first_seen"], "class": r["now"]["class"],
                   "tier": r["now"]["tier"], "rule": r["now"]["rule"]} for r in rows if r["now"]["class"] == want]
        data["listed"] = listed
        text += [f"  [{u['tier'] or '-'}] {u['name']}" for u in listed]
    return Result(data, "\n".join(text))


def _unit_row(db: Any, source: str, unit: str) -> dict[str, Any] | None:
    r = db.execute("SELECT * FROM units WHERE source=? AND unit_id=?", (source, unit)).fetchone()
    return dict(r) if r is not None else None


def _unknown_unit(db: Any, source: str, unit: str) -> InputError:
    from docket.config.loader import closest
    known = [r["unit_id"] for r in db.execute("SELECT unit_id FROM units WHERE source=?", (source,))]
    return InputError("input.unknown_unit", f"The registry has no unit {unit!r} of source {source!r}.",
                      hint="docket units list", details={"suggestion": closest(unit, known)})


@command("units set", "Classify one unit by hand. Only the attributes given change, and unit rules leave the "
         "unit alone from then on (classified_by: operator).",
         args=[Arg("source", "Source id."), Arg("unit", "Unit id."),
               Arg("--class", "The unit's class."), Arg("--tier", "The unit's tier."), Arg("--kind", "The unit's kind.")],
         effects=["writes one row of the unit registry"],
         examples=["docket units set chat spaces/room-1 --class social --tier 4 --json"])
def cmd_units_set(ctx: Any, ns: Any) -> Result:
    from docket.sources.units import set_classification
    ctx.workspace_root()
    values = {"class": getattr(ns, "class", None), "tier": ns.tier, "kind": ns.kind}
    if all(v is None for v in values.values()):
        raise InputError("input.bad_argument", "Give at least one of --class, --tier and --kind.",
                         hint="docket units set <source> <unit> --class <class>")
    db = ctx.store().db
    with db:
        if not set_classification(db, ns.source, ns.unit, values):
            raise _unknown_unit(db, ns.source, ns.unit)
    row = _unit_row(db, ns.source, ns.unit)
    return Result({"unit": row}, f"{ns.unit}: class {row['class'] or '-'}, tier {row['tier'] or '-'} (operator)")


@command("units unset", "Give one unit back to the unit rules and classify it by them now.",
         args=[Arg("source", "Source id."), Arg("unit", "Unit id.")],
         effects=["writes one row of the unit registry"],
         examples=["docket units unset chat spaces/room-1 --json"])
def cmd_units_unset(ctx: Any, ns: Any) -> Result:
    from docket.sources.units import unset_classification
    ws = ctx.workspace()
    db = ctx.store().db
    with db:
        if not unset_classification(db, ws, ns.source, ns.unit):
            raise _unknown_unit(db, ns.source, ns.unit)
    row = _unit_row(db, ns.source, ns.unit)
    return Result({"unit": row}, f"{ns.unit}: class {row['class'] or '-'}, tier {row['tier'] or '-'} "
                                 f"({row['classified_by'] or 'unclassified'})")


@command("ledger list", "List the item ledger: every item put in front of the operator, with its key, state and "
         "where it lives.", args=[Arg("--state", "Only items in this state (new, backlog, scheduled, raised, "
                                      "monitoring, acknowledged, done, ignored).")],
         examples=["docket ledger list --state backlog --json"])
def cmd_ledger_list(ctx: Any, ns: Any) -> Result:
    from collections import Counter

    from docket.items import ledger
    ctx.workspace_root()
    if ns.state and ns.state not in ledger.STATES:
        raise InputError("input.bad_argument", f"Unknown state {ns.state!r}.", hint="docket ledger list",
                         details={"states": list(ledger.STATES)})
    rows = ledger.rows(ctx.store().db, ns.state)
    counts = Counter(r["state"] for r in rows)
    text = [f"{len(rows)} item(s): " + ", ".join(f"{s} {counts[s]}" for s in ledger.STATES if counts[s])]
    text += [f"  {r['key']} {r['state']:<12} {(r['title'] or '')[:80]}" for r in rows]
    return Result({"items": rows, "states": {s: counts[s] for s in ledger.STATES if counts[s]}}, "\n".join(text))


@command("ledger show", "Show one ledger item by key or topic: its state, decisions and placements. Given a "
         "calendar block's key, show the items the block is for.",
         args=[Arg("key", "Item key, topic, or block key.")], examples=["docket ledger show i3k9q2ab --json"])
def cmd_ledger_show(ctx: Any, ns: Any) -> Result:
    from docket.items import ledger
    ctx.workspace_root()
    row = ledger.get(ctx.store().db, ns.key)
    if row is None:
        of_block = ledger.items_of_block(ctx.store().db, ns.key)   # a block's key: the items it is for
        if of_block:
            text = [f"block {ns.key} is for {len(of_block)} item(s):"] + \
                   [f"  {r['key']} {r['state']:<12} {(r['title'] or '')[:80]}" for r in of_block]
            return Result({"block": ns.key, "items": of_block}, "\n".join(text))
        raise InputError("input.unknown_item", f"The ledger has no item or block {ns.key!r}.", hint="docket ledger list")
    text = [f"{row['key']} {row['state']} {row['title'] or ''}", f"  {row['url'] or ''}"]
    text += [f"  {h.get('at', '')[:10]} {h.get('decision')} -> {h.get('state')}" + (f" ({h['note']})" if h.get("note") else "")
             for h in row["history"]]
    text += [f"  in {p.get('destination')}" + (f" under {p['anchor']}" if p.get("anchor") else "")
             + (f" {p['start']} to {p['end']}" if p.get("start") else "")
             + (f", one of {p['of']}" if p.get("of") else "")
             + (f", with {len(p['with'])} other(s)" if p.get("with") else "") for p in row["placements"]]
    return Result({"item": row}, "\n".join(text))


@command("ledger import", "Enter items into the ledger by hand, from a JSON array or JSON lines file. Each item "
         "needs a title; url, state (default backlog), due, est, area, source and note are kept when given; key, destination and adapter say where the item already lives. "
         "An item the ledger already has is skipped.",
         args=[Arg("file", "Path to the file, or - for stdin.")], mutating=True,
         effects=["writes ledger rows; never overwrites one"],
         examples=["docket ledger import old-items.json --json", "docket ledger import old-items.json --dry-run --json"])
def cmd_ledger_import(ctx: Any, ns: Any) -> Result:
    import json
    import sys
    from datetime import datetime, timezone

    from docket.items import ledger
    ctx.workspace_root()
    try:
        raw = sys.stdin.read() if ns.file == "-" else open(ns.file, encoding="utf-8").read()
    except OSError as e:
        raise InputError("input.bad_argument", f"Cannot read {ns.file}: {e.strerror}") from None
    try:
        data = json.loads(raw)
        items = data if isinstance(data, list) else data.get("items") if isinstance(data, dict) else None
    except ValueError:
        try:
            items = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        except ValueError:
            items = None
    if not isinstance(items, list):
        raise InputError("input.bad_argument", "The file must hold a JSON array of items, or one JSON object per line.")
    at = datetime.now(timezone.utc).isoformat()
    db = ctx.store().db
    db.execute("SAVEPOINT ledger_import")
    report = ledger.import_rows(db, items, run_id="import-" + at[:10], at=at)
    if getattr(ns, "dry_run", False):
        db.execute("ROLLBACK TO ledger_import")
        report["dry_run"] = True
    db.execute("RELEASE ledger_import")
    db.commit()
    return Result(report, f"imported {report['imported']}, skipped {report['skipped']}, problems {len(report['problems'])}")


@command("ledger reconcile", "Follow what happened in the destinations: an item whose task line was closed or "
         "removed there moves to done. Every durable seal does this; run it by hand to see or apply it sooner.",
         mutating=True, effects=["moves ledger items to done; reads destinations and writes none"],
         examples=["docket ledger reconcile --dry-run --json"])
def cmd_ledger_reconcile(ctx: Any, ns: Any) -> Result:
    from datetime import datetime, timezone

    from docket.items import ledger
    ctx.workspace_root()
    at = datetime.now(timezone.utc).isoformat()
    seen = ledger.read_back(ctx.workspace())
    dry = bool(getattr(ns, "dry_run", False))
    db = ctx.store().db
    moves = ledger.observe(db, seen, run_id="reconcile-" + at[:10], at=at, write=not dry)
    db.commit()
    unread = sorted(d for d, v in seen.items() if v is None)
    text = [f"{'would move' if dry else 'moved'} {len(moves)} item(s)"] + \
           [f"  {m['key']} {m['was']} -> {m['state']}: {m['note']}  {(m['title'] or '')[:70]}" for m in moves] + \
           [f"  could not read {d}" for d in unread]
    return Result({"moves": moves, "read": sorted(d for d, v in seen.items() if v is not None), "unread": unread,
                   "dry_run": dry}, "\n".join(text))
