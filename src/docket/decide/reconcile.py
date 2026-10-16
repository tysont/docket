"""Reconcile questions (TD §10.4, FR-DEC-05): did the planned work happen?

Candidates are owned task blocks (ownership key ``<prefix>t_...``) that ended
inside the reconcile window: from the later of ``run instant - lookback`` and
the instant of the workspace's last sealed live run, up to the run instant. A
block is asked about only when its task is still open in the task store, so a
block is reconciled once: by the next run its window has moved on, and a
closed task is never asked about again.

Option kinds are fixed (``done``, ``partly``, ``not_done``, ``to_agenda``,
``drop``); labels come from ``decisions.reconcile.labels``. At
``decisions.reconcile.carry_limit`` carries ``not_done`` is replaced by
``rescope``. Each answer maps to task-store operations previewed by the store
adapter (``task.close``, ``task.carry``, ``task.update``) and, for
``to_agenda``, an ``agenda.add``; the scheduler then follows the store (a
closed task's future blocks are released, a carried one is placed again).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from docket.config.digest import digest
from docket.decide.store import StoreTask, block_key, open_tasks
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

LABELS = {
    "done": "Done",
    "partly": "Partly: it needs more time (answer with --est for the remainder)",
    "not_done": "Did not happen: carry it",
    "rescope": "Re-scope it (carried {carry} times)",
    "to_agenda": "Move it to an agenda",
    "drop": "Drop it",
}
PROMPT = "Did \"{block.title}\" happen ({block.day} {block.start}-{block.end})?{?carried} Carried {carried} time(s) " \
         "so far.{/carried}"
ACTIONS = {"done": "reconcile.done", "partly": "reconcile.partly", "not_done": "reconcile.not_done",
           "rescope": "reconcile.rescope", "to_agenda": "reconcile.to_agenda", "drop": "reconcile.drop"}
REQUIRES = {"partly": ["est"], "rescope": ["est|title|action|due"], "to_agenda": []}


def _last_sealed_instant(rc: "RunContext") -> datetime | None:
    store = rc.run.store
    best: datetime | None = None
    for row in store.db.execute("SELECT run_id FROM runs WHERE status='sealed' AND sandbox=0 AND run_id<>?",
                                (rc.run.id,)):
        try:
            hdr = json.loads((store.runs_dir / row["run_id"] / "run.json").read_text(encoding="utf-8"))
            inst = datetime.fromisoformat(hdr["clock"]["instant"])
        except (OSError, ValueError, KeyError):
            continue
        if best is None or inst > best:
            best = inst
    return best


def window(rc: "RunContext") -> tuple[datetime, datetime]:
    from docket.text.durations import parse_duration
    now = datetime.fromisoformat(rc.run.header.clock.instant)
    lo = now - timedelta(minutes=parse_duration(rc.ws.config.decisions.reconcile.lookback))
    last = _last_sealed_instant(rc)
    if last is not None and lo < last < now:
        lo = last
    return lo, now


def candidates(rc: "RunContext", tasks: list[StoreTask] | None = None) -> list[dict[str, Any]]:
    """Elapsed owned blocks in the window whose task is open, oldest first."""
    from docket.plan.build import _provider
    cal = rc.ws.config.planning.calendar
    if cal is None:
        return []
    tz = ZoneInfo(rc.run.header.clock.timezone)
    lo, hi = window(rc)
    prefix = rc.ws.config.planning.key_prefix
    tasks = open_tasks(rc) if tasks is None else tasks
    by_key = {block_key(prefix, t.id): t for t in tasks}
    out = []
    from docket.shapes.unexpected import collecting
    with collecting():  # calendar shapes are recorded by plan build, not by every evaluation
        events = _provider(rc, tz).list_events(lo, hi)
    for ev in events:
        key = ev.owned_key or ""
        if ev.cancelled or ev.all_day or not isinstance(ev.start, datetime) or not key.startswith(prefix + "t_"):
            continue
        end = ev.end if isinstance(ev.end, datetime) else None
        if end is None or not (lo < end <= hi):
            continue
        task = by_key.get(key)
        if task is None:
            continue
        s, e = ev.start.astimezone(tz), end.astimezone(tz)
        out.append({"block": {"key": key, "title": ev.title, "event_id": ev.id, "start": s.strftime("%H:%M"),
                              "end": e.strftime("%H:%M"), "day": s.strftime("%a %Y-%m-%d"),
                              "start_at": s.isoformat(), "end_at": e.isoformat(),
                              "minutes": int((e - s).total_seconds() // 60)},
                    "task": task.view()})
    out.sort(key=lambda r: (r["block"]["start_at"], r["block"]["key"]))
    return out


def subject_digest(row: dict[str, Any]) -> str:
    b = row["block"]
    return digest({"key": b["key"], "start": b["start_at"], "end": b["end_at"], "line": row["task"]["line"]})


def context(rc: "RunContext", row: dict[str, Any]) -> dict[str, Any]:
    return {"block": row["block"], "task": row["task"], "carry": row["task"]["carry"],
            "carried": row["task"]["carry"] or "",
            "carry_limit": rc.ws.config.decisions.reconcile.carry_limit}


def question(rc: "RunContext", row: dict[str, Any]) -> dict[str, Any]:
    from docket.decide.options import default_destination
    cfg = rc.ws.config.decisions
    ctx = context(rc, row)
    labels = {**LABELS, **cfg.reconcile.labels}
    at_limit = row["task"]["carry"] >= cfg.reconcile.carry_limit
    ids = ["done", "partly", "rescope" if at_limit else "not_done", "to_agenda", "drop"]
    opts, refused = [], []
    for oid in ids:
        dest = row["task"]["destination"]
        if oid == "to_agenda":
            dest = default_destination(rc, "agenda.add", None)
            if dest is None:
                refused.append({"option": oid, "action": ACTIONS[oid], "reason": "no agenda destination is configured"})
                continue
        opts.append({"id": oid, "label": templates.render(labels[oid], ctx), "action": ACTIONS[oid],
                     "destination": dest, "defaults": {"est": row["task"]["est"]} if oid == "partly" else {}})
    notes = []
    if at_limit:
        notes.append(f"Carried {row['task']['carry']} times (limit {cfg.reconcile.carry_limit}): re-scope or drop "
                     f"it instead of carrying it again.")
    prompt = templates.render(cfg.templates.get("reconcile_prompt", PROMPT), ctx)
    return {"id": "r_" + row["block"]["key"], "kind": "reconcile", "block_key": row["block"]["key"],
            "task_id": row["task"]["id"], "prompt": prompt,
            "evidence": [f"Planned block {row['block']['day']} {row['block']['start']}-{row['block']['end']}",
                         f"Task line: {row['task']['line'].strip()}"],
            "options": opts, "refused": refused, "notes": notes, "requires": {k: v for k, v in REQUIRES.items() if v},
            "subject": subject_digest(row)}


def operations(rc: "RunContext", qid: str, row: dict[str, Any], answer: dict[str, Any]) -> tuple[
        list[dict[str, Any]], dict[str, Any]]:
    """(operations, store adjustment for planning) for one reconcile answer."""
    from docket.decide.operations import agenda_fields, preview, task_update_fields
    from docket.text.durations import format_duration
    oid = answer["option"]
    ov = answer.get("overrides") or {}
    task = row["task"]
    did = task["destination"]
    base = {"anchor": task["section"]}
    carry_name = rc.ws.config.destinations[did].carry_field or "carry"
    if oid in ("done", "drop"):
        return preview(rc, "task.close", did, base, qid, target=task["line"]), {"closed": True}
    if oid == "not_done":
        return preview(rc, "task.carry", did, base, qid, target=task["line"]), {}
    if oid == "partly":
        sets = {**task_update_fields(rc, ov), carry_name: str(task["carry"] + 1)}
        return preview(rc, "task.update", did, {**base, "set": sets}, qid, target=task["line"]), \
            {"est": ov.get("est"), "est_text": format_duration(ov.get("est"))}
    if oid == "rescope":
        sets = {**task_update_fields(rc, ov), carry_name: "0"}
        fields = {**base, "set": sets}
        title = ov.get("action") or ov.get("title")
        if title:
            fields["title"] = title
        adj: dict[str, Any] = {}
        if ov.get("est"):
            adj["est"] = ov["est"]
        return preview(rc, "task.update", did, fields, qid, target=task["line"]), adj
    if oid == "to_agenda":
        from docket.decide.options import default_destination
        agenda = default_destination(rc, "agenda.add", None)
        ops = preview(rc, "agenda.add", agenda, agenda_fields(rc, task["title"], ov), qid) if agenda else []
        ops += preview(rc, "task.close", did, base, qid, target=task["line"])
        return ops, {"closed": True}
    return [], {}


__all__ = ["candidates", "question", "operations", "window", "LABELS"]
