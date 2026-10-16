"""``docket unexpected`` and ``docket unexpected accept`` (TD §14.2).

An acceptance lives in two places with one meaning: ``shapes.accepted`` in the
workspace configuration (reviewable, committed with the workspace) and the
``unexpected_accepted`` table that ``unexpected accept`` writes for the
operator's convenience. Either hides the shape from reports; ``--all`` shows
it with where it was accepted. ``unexpected accept`` prints the configuration
entry to commit, because Docket never writes inside the workspace itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from docket.cli.envelope import Result
from docket.cli.registry import Arg, command


def config_acceptances(ctx: Any) -> dict[tuple[str, str], str]:
    """``shapes.accepted`` from the workspace configuration; empty when it does not load."""
    from docket.cli.errors import DocketError
    try:
        ws = ctx.workspace()
    except DocketError:
        return {}
    return {(a.area, a.detail): a.reason for a in ws.config.shapes.accepted}


def config_entry(area: str, detail: str, reason: str) -> str:
    import json
    return (f"shapes:\n  accepted:\n    - area: {json.dumps(area)}\n      detail: {json.dumps(detail)}\n"
            f"      reason: {json.dumps(reason)}\n")


@command("unexpected", "Aggregate unexpected shapes across runs, most frequent first.",
         args=[Arg("--since", "Only shapes recorded within this duration, for example 7d or 12h."),
               Arg("--all", "Include accepted shapes (shapes.accepted or unexpected accept).", action="store_true")],
         examples=["docket unexpected --json", "docket unexpected --since 7d --json"])
def cmd_unexpected(ctx: Any, ns: Any) -> Result:
    from docket.text.durations import parse_duration
    ctx.workspace_root()
    db = ctx.store().db
    where, params = "", []
    if ns.since:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=parse_duration(ns.since))
        where, params = "WHERE u.recorded_at >= ?", [cutoff.isoformat()]
    rows = db.execute(
        "SELECT u.area, u.detail, COUNT(*) AS count, MIN(u.recorded_at) AS first_seen, MAX(u.recorded_at) AS last_seen, "
        "MAX(u.reading) AS reading, MAX(u.extend) AS extend, COUNT(DISTINCT u.run_id) AS runs, "
        "a.reason AS accepted_reason "
        "FROM unexpected u LEFT JOIN unexpected_accepted a ON a.area = u.area AND a.detail = u.detail "
        f"{where} GROUP BY u.area, u.detail ORDER BY count DESC, u.area, u.detail", params).fetchall()
    in_config = config_acceptances(ctx)
    shapes = []
    for r in rows:
        s = dict(r)
        key = (s["area"], s["detail"])
        if key in in_config:
            s["accepted_reason"], s["accepted_in"] = in_config[key], "config"
        elif s["accepted_reason"] is not None:
            s["accepted_in"] = "database"
        else:
            s["accepted_in"] = None
        if ns.all or s["accepted_in"] is None:
            shapes.append(s)
    hidden = sum(1 for r in rows) - len(shapes) if not ns.all else 0
    text = "\n".join(f"{s['count']:>4}  {s['area']} / {s['detail']}  -> extend: {s['extend']}"
                     + (f"  [accepted: {s['accepted_reason']}]" if s["accepted_in"] else "") for s in shapes)
    return Result({"shapes": shapes, "total": len(shapes), "accepted_hidden": hidden},
                  text or "no unexpected shapes")


@command("unexpected accept", "Mark a shape as known and reviewed so reports stop listing it.", mutating=True,
         args=[Arg("area", "Shape area."), Arg("detail", "Shape detail key."),
               Arg("--reason", "Why the shape is acceptable.", required=True)],
         effects=["writes an acceptance row to the workspace database",
                  "prints the shapes.accepted entry to commit to the workspace configuration"],
         examples=['docket unexpected accept source.kind "codemode not implemented" --reason "M2" --json'])
def cmd_unexpected_accept(ctx: Any, ns: Any) -> Result:
    ctx.workspace_root()
    entry = config_entry(ns.area, ns.detail, ns.reason)
    in_config = (ns.area, ns.detail) in config_acceptances(ctx)
    if ctx.dry_run:
        return Result({"dry_run": True, "area": ns.area, "detail": ns.detail, "config_entry": entry},
                      "would accept the shape")
    db = ctx.store().db
    with db:
        db.execute("INSERT OR REPLACE INTO unexpected_accepted (area, detail, reason, accepted_at) VALUES (?,?,?,?)",
                   (ns.area, ns.detail, ns.reason, datetime.now(timezone.utc).isoformat()))
    note = "" if in_config else f"\nmake it reviewable: add to the workspace configuration\n{entry}"
    return Result({"area": ns.area, "detail": ns.detail, "reason": ns.reason, "accepted": True,
                   "in_config": in_config, "config_entry": entry},
                  f"accepted {ns.area} / {ns.detail}" + note)
