"""Auditing agendas (TD §9.7, §10.7, §12.8).

A forum that meets again and again keeps one agenda. What flows into it is what happened to
be raised; an audit asks what the next meeting should hold. A ``judge: audit`` node picks the
forums to audit in a run, builds one pack item per forum, and hands on the proposals the
``audit`` task returns. An ask node with the ``audit`` question kind puts each proposal that
changes something to the operator (``docket.decide.audit``), and a ``write: audit`` node turns
the answers into edits of the agenda destination.

Which forums: a forum with a person (``profile.forums[].person``) whose note a roll wrote in
this run or in a run since the last sealed live run (``after_meeting``), or whose next meeting
starts within ``before_meeting`` of working time from the run's instant; and any forum the node
names in ``forums``. The next meeting is the earliest one ahead of the run's instant among the
meetings the node's inputs hand on and the events of the planning calendar, each matched to its
forum as a roll matches a meeting (``forum_for_meeting``). A forum whose note does not exist is
left out. A forum audited once is not audited again for the same pack: the pack's digest is the
task's input digest, so an unchanged forum reuses its result.

The pack item of a forum carries the forum and the person with what the profile says of them
(``about``, ``roles``, ``reports_to``), the agenda section's entries (N1, N2, ... as
``Agenda.entries`` names them), the note's latest dated sections, each cut to a size, the dates
of the last and the next meeting, and the related ledger items, most recent first and capped.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from docket.config.digest import digest
from docket.shapes.unexpected import unexpected

if TYPE_CHECKING:
    from docket.engine.context import RunContext

LOOKAHEAD_DAYS = 14          # how far ahead the next meeting is looked for, at least
_DATED = re.compile(r"^#{1,6}\s+(\d{4}-\d{2}-\d{2})\b")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --- time -------------------------------------------------------------------------------


def working_until(now: datetime, minutes: int, workdays: list[str]) -> datetime:
    """The instant ``minutes`` of working time after ``now``: only the hours of working days count,
    so two days from a Friday morning is the Tuesday morning after."""
    if minutes <= 0 or not workdays:
        return now
    t, left = now, float(minutes)
    for _ in range(3660):
        nxt = datetime.combine(t.date() + timedelta(days=1), time(0), tzinfo=t.tzinfo)
        span = (nxt - t).total_seconds() / 60
        if _DAYS[t.weekday()] in workdays:
            if span >= left:
                return t + timedelta(minutes=left)
            left -= span
        t = nxt
    return t


def _instant(value: Any, tz: ZoneInfo) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


# --- forums -----------------------------------------------------------------------------


def forums_of(rc: "RunContext", to: str) -> list[Any]:
    """The forums whose agenda lives in destination ``to``."""
    return [f for f in rc.ws.config.profile.forums
            if (f.agenda or {}).get("path") and (f.agenda or {}).get("destination") in (None, to)]


def rolled_paths(rc: "RunContext", to: str) -> set[str]:
    """The agenda files of ``to`` a roll was written to in this run, or in a live run that began
    since the last sealed live run (a run abandoned or still open after it)."""
    from docket.store.runs import load_run
    store = rc.run.store
    me = rc.run.id
    rows = list(store.list_runs())
    sealed = [r["created_at"] for r in rows if r["status"] == "sealed" and not r["sandbox"] and r["run_id"] != me]
    since = max(sealed) if sealed else None
    runs = [rc.run]
    for r in rows:
        if r["run_id"] == me or r["sandbox"] or r["status"] == "sealed":
            continue
        if since is None or r["created_at"] > since:
            try:
                runs.append(load_run(store, r["run_id"]))
            except Exception:  # a run whose directory is gone wrote nothing we can read
                continue
    out: set[str] = set()
    for run in runs:
        applied: dict[str, str] = {}
        rp = run.paths.receipts
        if rp.is_file():
            for ln in rp.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    row = json.loads(ln)
                    applied[row.get("op_id")] = row.get("outcome")
        pdir = run.dir / "plan"
        if not pdir.is_dir():
            continue
        for plan in sorted(pdir.glob("*/plan.json")):
            for op in json.loads(plan.read_text(encoding="utf-8")).get("operations") or []:
                if op.get("verb") == "roll" and op.get("title") and op.get("destination") == to \
                        and applied.get(op.get("op_id")) == "applied":
                    out.add(str(op.get("path")))
    return out


def meetings(rc: "RunContext", items: list[dict[str, Any]], forums: list[Any], until: datetime) -> tuple[
        dict[str, datetime], dict[str, str], str | None]:
    """(the next meeting of each forum up to ``until``, the date of its last meeting, why the
    planning calendar could not say what is ahead or None), from the meetings the node's inputs
    hand on and from the planning calendar when there is one."""
    from docket.plan.agenda_roll import _me, forum_for_meeting
    tz = ZoneInfo(rc.run.header.clock.timezone)
    from docket.engine.clock import pinned_now
    now = pinned_now(rc.run.header.clock).astimezone(tz)
    me = _me(rc)
    nxt: dict[str, datetime] = {}
    last: dict[str, str] = {}
    gap: str | None = None

    def ahead(fid: str, at: datetime) -> None:
        if now < at <= until and (fid not in nxt or at < nxt[fid]):
            nxt[fid] = at

    for it in items:
        at = _instant(it.get("at"), tz)
        f = forum_for_meeting(forums, str(it.get("title") or ""),
                              list((it.get("fields") or {}).get("attendees") or []), me)
        if f is None or at is None:
            continue
        if at > now:
            ahead(f.id, at)
        elif at.date().isoformat() > last.get(f.id, ""):
            last[f.id] = at.date().isoformat()
    try:
        from docket.plan.build import calendar_provider
        prov = calendar_provider(rc, tz)
        if prov is None:
            gap = "no planning calendar is configured"
        else:
            from docket.plan.calendar_classify import expand
            lo, hi = now.date(), until.date() + timedelta(days=1)
            events = prov.list_events(datetime.combine(lo, time(0), tzinfo=tz), datetime.combine(hi, time(0), tzinfo=tz))
            for ev in expand(events, lo, hi, tz).events:
                if ev.cancelled or ev.all_day or not isinstance(ev.start, datetime):
                    continue
                att = ev.properties.get("attendees")
                f = forum_for_meeting(forums, ev.title, [str(a) for a in att] if isinstance(att, list) else [], me)
                if f is not None:
                    ahead(f.id, ev.start.astimezone(tz))
    except Exception as e:  # the calendar is one way to know the next meeting; a failing one is reported
        gap = f"the planning calendar could not be read ({type(e).__name__})"
    return nxt, last, gap


# --- the pack item ----------------------------------------------------------------------


def person_view(rc: "RunContext", pid: str | None) -> dict[str, Any] | None:
    prof = rc.ws.config.profile
    p = next((x for x in prof.people if x.id == pid), None)
    if p is None:
        return None
    boss: Any = None
    if p.reports_to == "me":
        boss = {"id": "me", "name": prof.me.name}
    elif p.reports_to:
        b = next((x for x in prof.people if x.id == p.reports_to), None)
        boss = {"id": p.reports_to, "name": b.name if b else p.reports_to}
    return {"id": p.id, "name": p.name, "about": p.about or "", "roles": list(p.roles), "reports_to": boss}


def dated_sections(text: str, count: int, chars: int) -> list[dict[str, Any]]:
    """The note's latest ``count`` dated sections, newest first, each cut to ``chars`` characters."""
    from docket.destinations.markdown_io import Doc
    doc = Doc.parse(text)
    found = []
    for i, _ in doc.headings():
        m = _DATED.match(doc.lines[i])
        if m:
            found.append((m.group(1), i))
    out = []
    for d, i in sorted(found, key=lambda x: (x[0], x[1]), reverse=True)[:count]:
        body = "\n".join(ln for _, ln in (doc.body(doc.lines[i]) or [])).strip("\n")
        cut = len(body) > chars
        if cut:
            body = body[:chars].rsplit("\n", 1)[0] if "\n" in body[:chars] else body[:chars]
        out.append({"date": d, "heading": doc.lines[i].strip(), "text": body, **({"cut": True} if cut else {})})
    return out


def related_rows(db: Any, forum: Any, to: str, max_rows: int, rc: "RunContext") -> tuple[list[dict[str, Any]], int]:
    """(the ledger items that concern a forum, most recent first and at most ``max_rows``, and how
    many there are). An item concerns it when a placement put it on the forum's agenda, when the
    forum's person owns or sent it, when it routes to the forum, or when its area is the area of
    a topic discussed there."""
    from docket.items import ledger
    path = forum.agenda["path"]
    pid = forum.person
    areas = {t.area for t in rc.ws.config.profile.topics if t.forum == forum.id and t.area}
    hits = []
    for row in ledger.rows(db):
        why = []
        if any(p.get("path") == path and (p.get("destination") == to or p.get("adapter") == "markdown_agenda")
               for p in row["placements"]):
            why.append("agenda")
        if pid and row.get("owner") == pid:
            why.append("owner")
        if pid and row.get("sender") == pid:
            why.append("sender")
        if row.get("forum") == forum.id:
            why.append("forum")
        if row.get("area") and row["area"] in areas:
            why.append("area")
        if why:
            hits.append((row, why))
    hits.sort(key=lambda h: (str(h[0].get("state_at") or h[0].get("first_at") or ""), h[0]["key"]), reverse=True)
    out = []
    for row, why in hits[:max_rows]:
        out.append({"key": row["key"], "state": row["state"], "date": str(row.get("state_at") or "")[:10],
                    "ask": row.get("ask") or row.get("title") or "", "link": row.get("url") or "",
                    "owner": row.get("owner") or "", "from": row.get("sender") or "", "why": why})
    return out, len(hits)


def pack_item(rc: "RunContext", forum: Any, text: str, entries: list[dict[str, Any]], last: str | None,
              next_at: datetime | None) -> dict[str, Any]:
    cfg = rc.ws.config.tasks.audit
    rows, total = related_rows(rc.run.store.db, forum, str(cfg.agendas), cfg.max_ledger, rc)
    ledger: dict[str, Any] = {"items": rows, "shown": len(rows), "total": total}
    if total > len(rows):
        ledger["note"] = f"{total - len(rows)} older item(s) not shown; the {len(rows)} most recent are"
    fields = {
        "forum": {"id": forum.id, "name": forum.name or forum.id, "path": forum.agenda["path"]},
        "person": person_view(rc, forum.person),
        "entries": [{"line": e["line"], "text": e["text"], **({"lines": e["block"][1:]} if e["block"][1:] else {})}
                    for e in entries],
        "sections": dated_sections(text, cfg.sections, cfg.section_chars),
        "meetings": {"last": last, "next": next_at.isoformat() if next_at else None},
        "ledger": ledger,
    }
    return {"id": f"forum:{forum.id}", "source": "audit", "title": forum.name or forum.id, "text": "",
            "fields": fields}


def candidates(rc: "RunContext", node: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pack items of the forums node ``node`` audits in this run (see the module text)."""
    return selection(rc, node, items)["candidates"]


def selection(rc: "RunContext", node: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    from docket.config.models import AuditSettings
    from docket.destinations import for_run
    from docket.engine.clock import pinned_now
    from docket.text.durations import parse_duration
    cfg = rc.ws.config
    to = cfg.tasks.audit.agendas
    out: dict[str, Any] = {"candidates": [], "selected": {}, "skipped": []}
    if not to or to not in cfg.destinations:
        return out
    settings = rc.graph[node].audit or AuditSettings()
    forums = forums_of(rc, to)
    tz = ZoneInfo(rc.run.header.clock.timezone)
    now = pinned_now(rc.run.header.clock).astimezone(tz)
    horizon = working_until(now, parse_duration(settings.before_meeting), list(cfg.profile.workdays)) \
        if settings.before_meeting not in (None, "", 0) else None
    until = max(horizon or now, now + timedelta(days=LOOKAHEAD_DAYS))
    nxt, last, gap = meetings(rc, items, forums, until)
    if gap and horizon is not None:  # before_meeting cannot see what the calendar holds: say so
        out["upcoming"] = f"upcoming events unavailable: {gap}; the next meeting comes from the meetings read only"
        unexpected("plan.audit", "upcoming events unavailable", sample={"node": node, "reason": gap},
                   reading="know the next meeting from the meetings read only", extend="planning.calendar")
    rolled = rolled_paths(rc, to) if settings.after_meeting else set()
    dest = for_run(rc, to)
    try:
        files = dest.snapshot().data.get("files") or {}
        for f in forums:
            path = f.agenda["path"]
            why = []
            if f.person and path in rolled:
                why.append("after_meeting")
            if f.person and horizon is not None and f.id in nxt and nxt[f.id] <= horizon:
                why.append("before_meeting")
            if f.id in settings.forums:
                why.append("named")
            if not why:
                continue
            text = files.get(path)
            if text is None:
                out["skipped"].append({"forum": f.id, "path": path, "reason": "the note does not exist"})
                continue
            ld = dest.last_dated(text)
            lm = max([x for x in (last.get(f.id), ld) if x], default=None)
            out["candidates"].append(pack_item(rc, f, text, dest.entries(text), lm, nxt.get(f.id)))
            out["selected"][f.id] = why
    finally:
        dest.close()
    return out


def combine(rc: "RunContext", plan: Any, cands: list[dict[str, Any]]) -> dict[str, Any]:
    """What a judge: audit node hands on: one proposal per forum audited, with what the ask and
    write nodes need (the entries as the audit saw them, the ledger links, the destination)."""
    from docket.tasks.runner import rows
    got = rows(plan)
    to = rc.ws.config.tasks.audit.agendas
    out = []
    for c in cands:
        hit = got.get(c["id"])
        if hit is None:
            continue
        f = c["fields"]
        out.append({"forum": f["forum"]["id"], "forum_name": f["forum"]["name"], "path": f["forum"]["path"],
                    "person": ({"id": f["person"]["id"], "name": f["person"]["name"]} if f.get("person") else None),
                    "destination": to, "task": hit[0], "digest": digest(f),
                    "entries": [{"line": e["line"], "text": e["text"]} for e in f["entries"]],
                    "meetings": f["meetings"],
                    "ledger": [{"key": r["key"], "link": r["link"], "state": r["state"]} for r in f["ledger"]["items"]
                               if r.get("link")],
                    "row": {k: v for k, v in hit[1].items() if k != "id"}})
    return {"audits": sorted(out, key=lambda a: a["forum"])}


# --- writing ----------------------------------------------------------------------------


def write_operations(rc: "RunContext", to: str, decide: dict[str, Any]) -> dict[str, Any]:
    """The edits a ``write: audit`` node makes from the answers to audit questions, through the
    agenda destination ``to``: rewords and drops first, then additions in the order the audit
    ranked them, as many as the agenda's capacity holds once the drops are gone. An edit whose
    entry changed since the proposal is refused before it is written (``refused``), and the
    additions left out for capacity are named (``left_out``)."""
    from docket.apply import receipts
    from docket.cli.errors import IntegrityError
    from docket.decide import audit as qa
    from docket.destinations import for_run
    from docket.destinations.base import Action
    from docket.destinations.markdown_io import Doc
    answers = decide.get("answers") or {}
    rows = [r for r in decide.get("audit") or [] if r.get("question_id") in answers and r.get("destination") == to]
    out: dict[str, Any] = {"operations": [], "refused": [], "left_out": []}
    if not rows:
        return out
    dest = for_run(rc, to)
    try:
        state = dest.snapshot()
        files = state.data.get("files") or {}
        cap = dest.cfg.capacity
        for r in sorted(rows, key=lambda r: r["forum"]):
            qid = r["question_id"]
            rewords, drops, adds = qa.changes(r, answers[qid])
            if not (rewords or drops or adds):
                continue
            n = dest.items(Doc.parse(files.get(r["path"])), dest.section)
            room = None if cap is None else max(0, cap - (n - len(drops)))
            fit = adds if room is None else adds[:room]
            out["left_out"] += [{"forum": r["forum"], "path": r["path"], "id": a["id"], "text": a["text"],
                                 "reason": f"the agenda holds {cap} entries"} for a in adds[len(fit):]]
            base = {"forum": {"id": r["forum"]}, "ident": qid}
            actions = ([dict(base, edit="replace", old=e["text"], text=e["new"]) for e in rewords]
                       + [dict(base, edit="remove", old=e["text"]) for e in drops]
                       + [dict(base, edit="append", text=a["text"], url=a.get("link") or "", key=a.get("key"))
                          for a in fit])
            for fields in actions:
                act = Action(kind="agenda.edit", destination=to, fields=fields,
                             item_id=fields.get("key") or None)
                try:
                    ops = dest.preview(act, state)
                except IntegrityError as e:
                    out["refused"].append({"op_id": "op_" + digest([to, qid, fields])[:16], "destination": to,
                                           "verb": fields["edit"], "line": fields.get("old") or fields.get("text"),
                                           "error": e.body()})
                    continue
                for op in ops:
                    raw = op.dump()
                    prior = receipts.existing(rc.run, raw)
                    problem = None if prior is not None and prior["verify_status"] == "verified" \
                        else dest.edit_problem(op, state)
                    if problem:
                        err = IntegrityError("dest.line_changed", f"{to}: {problem}",
                                             hint="docket ask show " + qid,
                                             details={"destination": to, "path": op.path, "anchor": op.anchor,
                                                      "old_line": op.old_line})
                        out["refused"].append({**raw, "error": err.body()})
                    else:
                        out["operations"].append(raw)
    finally:
        dest.close()
    return out


__all__ = ["selection", "candidates", "combine", "write_operations", "working_until", "rolled_paths"]
