"""Rolling agendas after meetings (TD §12.8).

A forum that meets again and again keeps one agenda: what is next, then a dated section for
each meeting held. After a meeting the agenda is rolled: what was covered goes under the
meeting's date, and the entries it settled leave what is next. This module turns the
meetings a run collected into roll operations, through the agenda destination's own
``agenda.roll`` preview.

A meeting with a recap (the ``recap`` task read its record) is rolled with what the recap
says was covered. A meeting with no record is rolled only when it is the forum's latest:
the agenda as it then stands moves down whole, with a note saying so. Meetings are rolled
oldest first, each operation waiting on the one before, and a meeting on or before the
agenda's latest dated section is left alone.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from docket.destinations.base import Action
from docket.shapes.unexpected import unexpected
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext


def forum_for_meeting(forums: list[Any], title: str, attendees: list[str], me: set[str]) -> Any | None:
    """The forum a meeting belongs to. A forum whose ``calendar_match.attendee`` is the only
    other person present; else the first whose ``calendar_match.title_regex`` matches the
    title. A forum without an agenda is not one a meeting can be rolled into."""
    mine = {m.lower() for m in me}
    others = sorted({a.lower() for a in attendees if a} - mine)
    usable = [f for f in forums if (f.agenda or {}).get("path") and f.calendar_match]
    if len(others) == 1:
        for f in usable:
            who = f.calendar_match.get("attendee")
            if who and str(who).lower() == others[0]:
                return f
    for f in usable:
        rx = f.calendar_match.get("title_regex")
        if rx and re.search(rx, title or ""):
            return f
    return None


def _lines(recap: dict[str, Any]) -> list[str]:
    out = [f"- {str(c.get('text') or '').strip()}" for c in recap.get("covered") or [] if str(c.get("text") or "").strip()]
    acts = [str(a.get("text") if isinstance(a, dict) else a).strip() for a in recap.get("actions") or []]
    acts = [a for a in acts if a]
    if acts:
        out += ["- Action items:"] + [f"\t- {a}" for a in acts]
    return out


def roll_operations(dest: Any, state: Any, forums: list[Any], me: set[str], meetings: list[dict[str, Any]], *,
                    no_record: str, trailer: str) -> list[dict[str, Any]]:
    """Roll operations for ``meetings`` (``id``, ``date``, ``title``, ``attendees``, ``url``,
    ``recap`` or None), in the order they must be applied."""
    by_forum: dict[str, tuple[Any, dict[str, dict[str, Any]]]] = {}
    for m in meetings:
        f = forum_for_meeting(forums, str(m.get("title") or ""), list(m.get("attendees") or []), me)
        date = str(m.get("date") or "")[:10]
        if f is None or not date:
            continue
        days = by_forum.setdefault(f.id, (f, {}))[1]
        if date not in days or (m.get("recap") and not days[date].get("recap")):
            days[date] = m  # one meeting a day; the one with a record
    files = state.data.get("files") or {}
    out: list[dict[str, Any]] = []
    for fid in sorted(by_forum):
        f, days = by_forum[fid]
        path = f.agenda["path"]
        text = files.get(path)
        if text is None:
            unexpected("plan.roll", "forum agenda not found", sample={"forum": fid, "path": path},
                       reading="roll nothing for this forum", extend=f"profile.forums[{fid}].agenda.path")
            continue
        last = dest.last_dated(text)
        dates = sorted(d for d in days if last is None or d > last)
        remaining = dest.entries(text)
        prev: str | None = None
        for date in dates:
            m = days[date]
            fields: dict[str, Any] = {"forum": {"id": fid}, "date": date}
            if m.get("recap"):
                ids = {e["line"] for e in remaining}
                gone = [c.get("next") for c in m["recap"].get("covered") or [] if c.get("next") in ids]
                fields.update({"lines": _lines(m["recap"]), "remove": gone})
                if trailer and m.get("url"):
                    fields["trailer"] = templates.render(trailer, {"url": m["url"], "title": m.get("title") or ""})
                remaining = [e for e in remaining if e["line"] not in gone]
            elif date == dates[-1]:
                fields.update({"lines": [ln for e in remaining for ln in e["block"]],
                               "remove": [e["line"] for e in remaining], "suffix": no_record})
                remaining = []
            else:
                continue  # no record, and a later meeting follows: nothing is known of it
            ops = dest.preview(Action(kind="agenda.roll", destination=dest.id, fields=fields, item_id=str(m.get("id")),
                                      depends_on=[prev] if prev else []), state)
            for op in ops:
                out.append(op.dump())
                prev = op.op_id
    return out


def _local_date(rc: "RunContext", at: Any) -> str:
    """The meeting's date where the operator is: an evening meeting is not tomorrow's."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    s = str(at or "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    return dt.astimezone(ZoneInfo(rc.run.header.clock.timezone)).date().isoformat() if dt.tzinfo else s[:10]


def _me(rc: "RunContext") -> set[str]:
    return {str(v).lower() for v in (rc.ws.config.profile.me.handles or {}).values() if "@" in str(v)}


def with_agendas(rc: "RunContext", candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare the meetings among a stage's candidates for the ``recap`` task. A meeting is
    read only when it has a record, belongs to a forum with an agenda, and is later than that
    agenda's latest dated section; it then carries the agenda's entries (``fields.agenda``)
    and its forum. Other meetings are dropped from the candidates: no task has anything to
    say about them. Candidates of other sources pass through."""
    from docket.tasks.kinds import recap_sources
    sources = set(recap_sources(rc.ws.config.tasks))
    roll = getattr(rc.workflow, "roll", None)
    if not sources or roll is None or not any(c.get("source") in sources for c in candidates):
        return candidates
    from docket.destinations import for_run
    prof = rc.ws.config.profile
    dest = for_run(rc, roll.destination)
    try:
        files = dest.snapshot().data.get("files") or {}
        out = []
        for c in candidates:
            if c.get("source") not in sources:
                out.append(c)
                continue
            f = dict(c.get("fields") or {})
            forum = forum_for_meeting(list(prof.forums), str(c.get("title") or ""), list(f.get("attendees") or []), _me(rc))
            text = files.get(forum.agenda["path"]) if forum is not None else None
            if forum is None or text is None or not str(c.get("text") or "").strip():
                continue
            last = dest.last_dated(text)
            if last is not None and _local_date(rc, c.get("at")) <= last:
                continue
            f["forum"] = forum.id
            f["agenda"] = [{"id": e["line"], "text": e["text"]} for e in dest.entries(text)]
            out.append({**c, "fields": f})
        return out
    finally:
        dest.close()


def meetings_of(rc: "RunContext") -> list[dict[str, Any]]:
    """The run's meetings: items of the workflow's roll sources, each with the ``recap``
    task's row when one read the meeting's record."""
    roll = getattr(rc.workflow, "roll", None)
    if roll is None:
        return []
    ev = rc.eval_or_none("assess")
    items = list((ev.output or {}).get("items") or []) if ev is not None else []
    out = []
    for it in items:
        if it.get("source") not in roll.sources:
            continue
        f = it.get("fields") or {}
        recap = (it.get("tasks") or {}).get("recap") if isinstance(it.get("tasks"), dict) else None
        out.append({"id": it.get("id"), "date": _local_date(rc, it.get("at")), "title": it.get("title"),
                    "attendees": list(f.get("attendees") or []), "url": f.get("record_url") or it.get("url"),
                    "recap": recap})
    return out


def capture_commitments(path: Any, meetings: list[dict[str, Any]]) -> int:
    """Append what the operator agreed to do (recap actions owned by ``me``) to a JSON-lines
    file, one record each, skipping records already there. Returns how many were added."""
    import json
    from pathlib import Path
    path = Path(path)
    have = set()
    if path.is_file():
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                have.add(json.loads(ln).get("id"))
            except ValueError:
                continue
    rows = []
    for m in meetings:
        for n, a in enumerate((m.get("recap") or {}).get("actions") or [], 1):
            if not isinstance(a, dict) or str(a.get("owner") or "").strip().lower() != "me":
                continue
            rid = f"{m.get('id')}:{n}"
            if rid not in have and str(a.get("text") or "").strip():
                rows.append({"id": rid, "text": str(a["text"]).strip(), "meeting": m.get("title"), "date": m.get("date"),
                             "url": m.get("url")})
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def operations(rc: "RunContext") -> list[dict[str, Any]]:
    """The run's roll operations: the workflow's ``roll`` settings over its meetings."""
    roll = getattr(rc.workflow, "roll", None)
    if roll is None:
        return []
    from docket.destinations import for_run
    dest = for_run(rc, roll.destination)
    try:
        return roll_operations(dest, dest.snapshot(), list(rc.ws.config.profile.forums), _me(rc), meetings_of(rc),
                               no_record=roll.no_record, trailer=roll.trailer)
    finally:
        dest.close()
