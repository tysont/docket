"""Cursor commands (TD §13.4, §19): ``cursor list``, ``cursor reset`` and ``cursor import``.

A cursor says where the next read of a source, or of one of its units, starts. The seal
moves cursors forward only. ``cursor reset`` is the one way back: it makes the next run read
a source again from a point, or from its lookback. ``cursor import`` seeds cursors once
from a predecessor's watermarks.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError, IntegrityError, StateError
from docket.cli.registry import Arg, command


def _table(rows: list[list[str]]) -> list[str]:
    """Rows as aligned columns; the last column is not padded."""
    if not rows:
        return []
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join([c.ljust(w) for c, w in zip(r, widths)] + [r[-1]]).rstrip() for r in rows]


def _label(unit: str, name: str | None) -> str:
    return f"{unit} ({name})" if name and name != unit else unit


def _reads_from(cfg: Any) -> str:
    return f"lookback {cfg.cursor.lookback}" if cfg.cursor.lookback else "the start of the source"


def _unknown_source(ws: Any, sid: str, extra: list[str] | None = None) -> InputError:
    from docket.config.loader import closest
    known = sorted(set(ws.config.sources) | set(extra or []))
    return InputError("input.unknown_source", f"No source {sid!r}.", hint="docket source list",
                      details={"suggestion": closest(sid, known), "sources": known})


@command("cursor list", "Show where each source stands: its cursor per unit, the coverage it proved and the run "
         "that last moved it. A source with many units is summarised; --units lists them.",
         args=[Arg("--source", "Only this source."), Arg("--unit", "Only this unit."),
               Arg("--units", "List every unit's cursor, not a summary.", action="store_true"),
               Arg("--limit", "Units listed per source (default 20).", default="20")],
         effects=["reads cursors and the unit registry; writes nothing"],
         examples=["docket cursor list --json", "docket cursor list --source chat --units --limit 50"])
def cmd_cursor_list(ctx: Any, ns: Any) -> Result:
    ws = ctx.workspace()
    try:
        limit = max(1, int(ns.limit))
    except ValueError:
        raise InputError("input.bad_argument", f"--limit must be a number, not {ns.limit!r}.",
                         hint="docket cursor list --units --limit 50") from None
    from docket.sources.cursors import standing
    db = ctx.store().db
    stored = {r[0] for r in db.execute("SELECT source FROM cursors UNION SELECT source FROM processed_ids")}
    sids = sorted(set(ws.config.sources) | stored)
    if ns.source:
        if ns.source not in sids:
            raise _unknown_source(ws, ns.source, sorted(stored))
        sids = [ns.source]
    out, lines = [], [["SOURCE", "MODEL", "UNIT", "POSITION", "COVERAGE END", "LAST MOVED BY"]]
    for sid in sids:
        cfg = ws.config.sources.get(sid)
        model = cfg.cursor.effective if cfg else None
        row: dict[str, Any] = {"source": sid, "model": model, "lookback": cfg.cursor.lookback if cfg else None,
                               "configured": cfg is not None}
        if model == "id-scan":
            n, last = db.execute("SELECT COUNT(*), MAX(run_id) FROM processed_ids WHERE source=?", (sid,)).fetchone()
            row.update({"units": 0, "processed": n, "updated_run": last, "cursors": [], "omitted": 0, "coverage": None})
            out.append(row)
            lines.append([sid, "id-scan", "-", f"{n} id(s) processed", "-", last or "-"])
            continue
        curs = standing(db, sid, ns.unit)
        ends = sorted(c["coverage_end"] for c in curs if c["coverage_end"])
        latest = max(curs, key=lambda c: (c["coverage_end"] or "", c["position"] or ""), default=None)
        row.update({"units": len(curs), "coverage": {"earliest": ends[0], "latest": ends[-1]} if ends else None,
                    "updated_run": latest["updated_run"] if latest else None})
        listed = curs[:limit] if (len(curs) <= 1 or ns.units or ns.unit) else []
        row["cursors"], row["omitted"] = listed, len(curs) - len(listed)
        out.append(row)
        shown = (f"cadence ({model})" if cfg.cursor.model == "cadence" else model) if cfg else "not configured"
        if not curs:
            lines.append([sid, shown, "-", "no cursor", f"reads from {_reads_from(cfg)}" if cfg else "-", "-"])
            continue
        if len(curs) > 1:
            span = f"{ends[0][:10]} .. {ends[-1][:10]}" if ends else "-"
            lines.append([sid, shown, f"{len(curs)} units", "-", span, row["updated_run"] or "-"])
        for c in listed:
            first = len(curs) == 1
            lines.append([sid if first else "", shown if first else "", _label(c["unit"], c["name"]),
                          c["position"] or "-", c["coverage_end"] or "-", c["updated_run"] or "-"])
        if listed and row["omitted"]:
            lines.append(["", "", f"... {row['omitted']} more (--limit)", "", "", ""])
    text = _table(lines) if len(lines) > 1 else ["no sources"]
    return Result({"sources": out}, "\n".join(text))


def _open_reads(ctx: Any, sid: str) -> list[str]:
    """Open runs that are not sandbox runs and have already read ``sid``."""
    store = ctx.store()
    out = []
    for r in store.db.execute("SELECT run_id FROM runs WHERE status='open' AND sandbox=0 ORDER BY created_at"):
        rid = r["run_id"]
        read = (store.runs_dir / rid / "read" / sid / "outcome.json").is_file()
        proposed = store.db.execute("SELECT 1 FROM cursor_proposals WHERE run_id=? AND source=? LIMIT 1",
                                    (rid, sid)).fetchone() is not None
        if read or proposed:
            out.append(rid)
    return out


def _fmt(c: dict[str, Any] | None) -> str:
    if c is None:
        return "no cursor"
    parts = ([f"position {c['position']}"] if c.get("position") else []) + \
            ([f"coverage {c['coverage_end']}"] if c.get("coverage_end") else [])
    return ", ".join(parts) or "empty cursor"


@command("cursor reset", "Make the next run read a source again from a point. With --to, set the cursor there; "
         "without it, remove the cursor so the source's lookback applies. An id-scan source forgets the ids it "
         "processed instead. Refused while an open run that is not a sandbox run has already read the source.",
         args=[Arg("source", "Source id."),
               Arg("--unit", "Only this unit (for an id-scan source, this record id)."),
               Arg("--to", "A date (start of the day in the profile's timezone), an ISO time, or a position.",
                   metavar="POINT")],
         mutating=True,
         effects=["writes or removes durable cursors, or forgets processed ids (one transaction)",
                  "appends each change to the cursor_resets ledger"],
         examples=["docket cursor reset chat --dry-run", "docket cursor reset chat --unit spaces/room-1 --to 2026-10-12",
                   "docket cursor reset mail --to 2026-10-14T06:00:00Z --json"])
def cmd_cursor_reset(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import closest
    from docket.sources.cursors import parse_point, plan_reset, write_reset
    ws = ctx.workspace()
    sid = ns.source
    if sid not in ws.config.sources:
        raise _unknown_source(ws, sid)
    cfg = ws.config.sources[sid]
    model = cfg.cursor.effective
    db = ctx.store().db
    runs = _open_reads(ctx, sid)
    if runs:
        rid = runs[0]
        raise StateError("cursor.read_in_open_run",
                         f"Run {rid} has already read {sid}; its seal would move the cursor past the reset.",
                         hint=f"Seal it first, then reset (docket seal --run {rid}); or abandon it: "
                              f"docket run abandon {rid}",
                         details={"source": sid, "runs": runs})
    if ns.unit is not None:
        if model == "id-scan":
            known = [r[0] for r in db.execute("SELECT record_id FROM processed_ids WHERE source=?", (sid,))]
        else:
            known = sorted({r[0] for r in db.execute("SELECT unit FROM cursors WHERE source=? UNION "
                                                     "SELECT unit_id FROM units WHERE source=?", (sid, sid))}
                           | ({"*"} if model != "units" else set()))
        if ns.unit not in known:
            raise InputError("input.unknown_unit", f"Source {sid!r} has no unit {ns.unit!r} with a cursor or in the "
                                                   "unit registry.", hint=f"docket cursor list --source {sid} --units",
                             details={"suggestion": closest(ns.unit, known), "source": sid})
    point = None
    if ns.to is not None:
        point = parse_point(ns.to, tz=ws.config.profile.timezone, now=datetime.now(timezone.utc),
                            token=cfg.cursor.position == "token")
        if model == "id-scan" and point[0] == "position":
            raise InputError("input.bad_argument", "An id-scan source keeps no position; give a date to forget the "
                                                   "ids processed since, or no --to to forget them all.",
                             hint=f"docket cursor reset {sid} --to 2026-10-12")
    plan = plan_reset(db, sid, model, unit=ns.unit, point=point)
    dry = ctx.dry_run
    if not dry and (plan["changes"] or plan["forgets"]):
        with db:
            write_reset(db, sid, plan, f"reset:{datetime.now(timezone.utc).date().isoformat()}")
    changed = len(plan["forgets"]) if model == "id-scan" else len(plan["changes"])
    data = {"source": sid, "model": model, "unit": ns.unit, "to": point[1] if point else None,
            "changes": plan["changes"], "changed": changed, "unchanged": len(plan["unchanged"]),
            "forgets": plan["forgets"], "kept": plan["kept"],
            "reads_from": point[1] if point else _reads_from(cfg), "dry_run": dry}
    verb = "would reset" if dry else "reset"
    if model == "id-scan":
        text = [f"{verb} {sid}: {changed} processed id(s) forgotten, {plan['kept']} kept"]
        text += [f"  {f['id']}  (processed by {f['run']})" for f in plan["forgets"][:20]]
        if len(plan["forgets"]) > 20:
            text.append(f"  ... {len(plan['forgets']) - 20} more")
        text.append(f"the next read of {sid} reads the forgotten ids again" if changed else "nothing to change")
        return Result(data, "\n".join(text))
    what = "removed" if point is None else "set"
    head = f"{verb} {sid}: {changed} cursor(s) {what}" + (f", {data['unchanged']} unchanged" if data["unchanged"] else "")
    rows = [[f"  {_label(c['unit'], c['name'])}", f"{_fmt(c['before'])} -> {_fmt(c['after'])}"]
            for c in plan["changes"][:20]]
    text = [head] + _table(rows) if rows else [head]
    if len(plan["changes"]) > 20:
        text.append(f"  ... {len(plan['changes']) - 20} more")
    if changed:
        text.append(f"the next read of {sid} starts from {data['reads_from']}")
    return Result(data, "\n".join(text))


@command("cursor import", "Seed durable cursors from a JSON file of source, unit and position rows.", mutating=True,
         args=[Arg("--from", "JSON file: [{source, unit, position, coverage_end}], {\"cursors\": [...]} or "
                             "{source: position | {unit: position}}.", required=True, metavar="FILE")],
         effects=["writes durable cursors (one transaction; nothing on any error)"],
         examples=["docket cursor import --from watermarks.json --json",
                   "docket cursor import --from watermarks.json --dry-run --json"])
def cmd_cursor_import(ctx: Any, ns: Any) -> Result:
    """A predecessor kept its watermarks elsewhere; importing them lets the first run read
    only what is new. The import is all or nothing. Cursors only move forward: a position
    behind the durable cursor refuses the whole import with ``cursor.regression``; a
    position equal to it is reported as unchanged."""
    from docket.config.loader import closest
    from docket.sources.cursors import parse_import, plan_import, write_import
    ws = ctx.workspace()
    path = Path(getattr(ns, "from"))
    if not path.is_absolute():
        path = ctx.cwd / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise InputError("input.bad_argument", f"Cannot read {path}: {e.strerror or e}.",
                         hint="docket cursor import --from <file>") from None
    except ValueError as e:
        raise InputError("input.bad_json", f"{path.name} is not JSON: {e}.", hint="docket help cursor import") from None
    rows, problems = parse_import(data)
    known = sorted(ws.config.sources)
    for r in rows:
        if r["source"] not in ws.config.sources:
            sugg = closest(r["source"], known)
            problems.append(f"{r['source']}: no such source" + (f" (did you mean {sugg!r}?)" if sugg else ""))
    if problems:
        raise InputError("input.bad_cursor_file", f"{len(problems)} problem(s) in {path.name}; nothing was imported.",
                         hint="docket help cursor import", details={"problems": problems, "sources": known})
    db = ctx.store().db
    plan = plan_import(db, rows)
    if plan["behind"]:
        raise IntegrityError("cursor.regression",
                             f"{len(plan['behind'])} imported position(s) are behind the durable cursor; cursors only "
                             f"move forward, so nothing was imported.",
                             hint="Remove those rows from the file, then import again; to go back on purpose, "
                                  "docket cursor reset <source> --to <point>",
                             details={"rows": plan["behind"]})
    summary = f"{len(plan['write'])} cursor(s) to write, {len(plan['same'])} unchanged"
    if ctx.dry_run:
        return Result({"dry_run": True, "would_write": plan["write"], "unchanged": plan["same"]}, "would import: " + summary)
    with db:
        write_import(db, plan["write"], "import:" + path.name)
    return Result({"written": plan["write"], "unchanged": plan["same"]}, "imported: " + summary)
