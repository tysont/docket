"""``docket unexpected`` and ``docket unexpected accept`` (TD §14.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from docket.cli.envelope import Result
from docket.cli.registry import Arg, command


@command("unexpected", "Aggregate unexpected shapes across runs, most frequent first.",
         args=[Arg("--since", "Only shapes recorded within this duration, for example 7d or 12h."),
               Arg("--all", "Include accepted shapes.", action="store_true")],
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
    shapes = [dict(r) for r in rows if ns.all or r["accepted_reason"] is None]
    text = "\n".join(f"{s['count']:>4}  {s['area']} / {s['detail']}  -> extend: {s['extend']}" for s in shapes)
    return Result({"shapes": shapes, "total": len(shapes)}, text or "no unexpected shapes")


@command("unexpected accept", "Mark a shape as known and reviewed so reports stop listing it.", mutating=True,
         args=[Arg("area", "Shape area."), Arg("detail", "Shape detail key."),
               Arg("--reason", "Why the shape is acceptable.", required=True)],
         effects=["writes an acceptance row to the workspace database"],
         examples=['docket unexpected accept source.kind "codemode not implemented" --reason "M2" --json'])
def cmd_unexpected_accept(ctx: Any, ns: Any) -> Result:
    ctx.workspace_root()
    if ctx.dry_run:
        return Result({"dry_run": True, "area": ns.area, "detail": ns.detail}, "would accept the shape")
    db = ctx.store().db
    with db:
        db.execute("INSERT OR REPLACE INTO unexpected_accepted (area, detail, reason, accepted_at) VALUES (?,?,?,?)",
                   (ns.area, ns.detail, ns.reason, datetime.now(timezone.utc).isoformat()))
    return Result({"area": ns.area, "detail": ns.detail, "reason": ns.reason, "accepted": True},
                  f"accepted {ns.area} / {ns.detail}")
