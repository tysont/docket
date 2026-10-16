"""``docket import cursors --from <json>`` (TD §19): seed durable cursors once.

A predecessor kept its watermarks elsewhere; importing them lets the first
run read only what is new. The import is all or nothing. Cursors only move
forward: a position behind the durable cursor refuses the whole import with
``cursor.regression``; a position equal to it is reported as unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError, IntegrityError
from docket.cli.registry import Arg, command


@command("import cursors", "Seed durable cursors from a JSON file of source, unit and position rows.", mutating=True,
         args=[Arg("--from", "JSON file: [{source, unit, position, coverage_end}], {\"cursors\": [...]} or "
                             "{source: position | {unit: position}}.", required=True, metavar="FILE")],
         effects=["writes durable cursors (one transaction; nothing on any error)"],
         examples=["docket import cursors --from watermarks.json --json",
                   "docket import cursors --from watermarks.json --dry-run --json"])
def cmd_import_cursors(ctx: Any, ns: Any) -> Result:
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
                         hint="docket import cursors --from <file>") from None
    except ValueError as e:
        raise InputError("input.bad_json", f"{path.name} is not JSON: {e}.", hint="docket help import cursors") from None
    rows, problems = parse_import(data)
    known = sorted(ws.config.sources)
    for r in rows:
        if r["source"] not in ws.config.sources:
            sugg = closest(r["source"], known)
            problems.append(f"{r['source']}: no such source" + (f" (did you mean {sugg!r}?)" if sugg else ""))
    if problems:
        raise InputError("input.bad_cursor_file", f"{len(problems)} problem(s) in {path.name}; nothing was imported.",
                         hint="docket help import cursors", details={"problems": problems, "sources": known})
    db = ctx.store().db
    plan = plan_import(db, rows)
    if plan["behind"]:
        raise IntegrityError("cursor.regression",
                             f"{len(plan['behind'])} imported position(s) are behind the durable cursor; cursors only "
                             f"move forward, so nothing was imported.",
                             hint="Remove those rows from the file, then import again",
                             details={"rows": plan["behind"]})
    summary = f"{len(plan['write'])} cursor(s) to write, {len(plan['same'])} unchanged"
    if ctx.dry_run:
        return Result({"dry_run": True, "would_write": plan["write"], "unchanged": plan["same"]}, "would import: " + summary)
    with db:
        write_import(db, plan["write"], "import:" + path.name)
    return Result({"written": plan["write"], "unchanged": plan["same"]}, "imported: " + summary)
