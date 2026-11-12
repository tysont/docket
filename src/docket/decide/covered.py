"""Covered questions (TD §10.4, §12.8, FR-DEC-09): did a meeting with no record cover its agenda?

An ask node that lists ``covered`` in ``questions.kinds`` names the read node of the meetings
and the agenda destination (``questions.covered``). For each forum it takes the meeting a
roll would take (``docket.plan.agenda_roll.by_forum_day``: a meeting read with a date, whose
forum has an agenda in that destination), and asks only when that meeting is newer than the
agenda's latest dated section, is the latest such meeting of its forum, left no record (its
text is empty), and the agenda section has entries. Then each entry is one question.

Option kinds are fixed (``covered``, ``keep``, ``drop``); labels come from
``decisions.covered.labels``. ``covered`` is recommended and is a batch option, so a driver
shows one meeting's entries together and records the ones not covered one by one. A
question's id derives from the forum, the meeting's date and the entry's text, so it is
stable for the same meeting and entry and changes when the entry is reworded.

The plan node turns the answers of one meeting into one ``agenda.roll``: the covered entries
(whole blocks) go under the meeting's date with the suffix that says there was no record,
covered and dropped entries leave the agenda section, kept ones stay. Nothing covered and
something dropped only removes; everything kept is no operation. A kept entry is remembered
at a durable seal (a ``keep`` disposition keyed by its question id), so the same meeting does
not ask about it again; the forum's next meeting has new ids and asks afresh.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from docket.config.digest import digest
from docket.destinations.base import Action
from docket.text import templates
from docket.text.grammar import web_links

if TYPE_CHECKING:
    from docket.engine.context import RunContext

OPTIONS = ("covered", "keep", "drop")
LABELS = {
    "covered": "Covered: it moves under {meeting.date}",
    "keep": "Not covered: keep it for the next meeting",
    "drop": "Drop it: remove it without a record",
}
PROMPT = "Did you cover this with {forum.name} on {meeting.date}? \"{entry.text}\""
ACTIONS = {"covered": "covered.covered", "keep": "covered.keep", "drop": "covered.drop"}
_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def rows(dest: Any, state: Any, forums: list[Any], me: set[str], meetings: list[dict[str, Any]], *, to: str,
         suffix: str, skip: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """One row per agenda entry to ask about, forum by forum, entries in order. ``meetings``
    are ``id``, ``date``, ``title``, ``attendees`` and ``record``; ``skip`` holds question ids
    already answered ``keep`` for their meeting."""
    from docket.plan.agenda_roll import by_forum_day, has_record
    files = state.data.get("files") or {}
    out: list[dict[str, Any]] = []
    for fid, (forum, days) in sorted(by_forum_day(forums, me, meetings).items()):
        if (forum.agenda or {}).get("destination") not in (None, to):
            continue
        path = forum.agenda["path"]
        text = files.get(path)
        if text is None:
            continue
        last = dest.last_dated(text)
        dates = sorted(d for d in days if last is None or d > last)
        if not dates or has_record(days[dates[-1]]):
            continue
        m = days[dates[-1]]
        meeting = {"id": str(m.get("id")), "date": dates[-1], "title": str(m.get("title") or ""), "forum": fid,
                   "forum_name": forum.name or fid, "path": path}
        seen: dict[str, int] = {}
        for e in dest.entries(text):
            n = seen.get(e["text"], 0)
            seen[e["text"]] = n + 1
            row = {"meeting": meeting, "entry": {"text": e["text"], "block": list(e["block"]), "line": e["line"], "n": n},
                   "destination": to, "suffix": suffix}
            if question_id(row) not in skip:
                out.append(row)
    return out


def question_id(row: dict[str, Any]) -> str:
    m, e = row["meeting"], row["entry"]
    return "c_" + digest(["covered", m["forum"], m["date"], e["text"].rstrip(), e.get("n", 0)])[:16]


def subject_digest(row: dict[str, Any]) -> str:
    m, e = row["meeting"], row["entry"]
    return digest({"forum": m["forum"], "date": m["date"], "meeting": m["id"], "block": e["block"]})


def meetings_of(rc: "RunContext", items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items a read node handed on, as meetings: a meeting has a record when its text is not empty."""
    from docket.plan.agenda_roll import _local_date
    return [{"id": it.get("id"), "date": _local_date(rc, it.get("at")), "title": it.get("title"),
             "attendees": list((it.get("fields") or {}).get("attendees") or []),
             "record": bool(str(it.get("text") or "").strip())} for it in items]


def remembered(rc: "RunContext") -> set[str]:
    """Question ids an earlier durable seal recorded as kept for their meeting."""
    return {r["topic_id"] for r in rc.run.store.db.execute(
        "SELECT topic_id FROM dispositions WHERE decision='keep' AND topic_id LIKE 'c\\_%' ESCAPE '\\' "
        "AND (run_id IS NULL OR run_id<>?)", (rc.run.id,))}


def candidates(rc: "RunContext", node: Any) -> list[dict[str, Any]]:
    """The rows the ask node ``node`` asks about (its ``questions.covered`` settings)."""
    from docket.destinations import for_run
    from docket.plan.agenda_roll import _me
    cov = node.questions.covered
    if cov is None:
        return []
    ev = rc.eval_or_none(cov.meetings)
    items = list(((ev.output if ev is not None and ev.state == "current" else None) or {}).get("items") or [])
    meetings = meetings_of(rc, items)
    if not meetings:
        return []
    dest = for_run(rc, cov.to)
    try:
        return rows(dest, dest.snapshot(), list(rc.ws.config.profile.forums), _me(rc), meetings, to=cov.to,
                    suffix=cov.suffix, skip=remembered(rc))
    finally:
        dest.close()


def context(row: dict[str, Any]) -> dict[str, Any]:
    m, e = row["meeting"], row["entry"]
    return {"forum": {"id": m["forum"], "name": m["forum_name"]},
            "meeting": {"id": m["id"], "date": m["date"], "title": m["title"]},
            "entry": {"text": _MARK.sub("", e["text"]).strip(), "line": e["line"]}}


def question(rc: "RunContext", row: dict[str, Any]) -> dict[str, Any]:
    cfg = rc.ws.config.decisions
    ctx = context(row)
    labels = {**LABELS, **cfg.covered.labels}
    opts = [{"id": oid, "label": templates.render(labels[oid], ctx), "action": ACTIONS[oid],
             "destination": row["destination"], "defaults": {}, **({"batch": True} if oid == "covered" else {})}
            for oid in OPTIONS]
    return {"id": question_id(row), "kind": "covered", "prompt": templates.render(cfg.templates.get("covered_prompt", PROMPT), ctx),
            "evidence": list(row["entry"]["block"]), "options": opts, "refused": [], "notes": [],
            "recommended": "covered", "meeting": dict(row["meeting"]), "subject": subject_digest(row),
            **web_links(row["entry"]["block"])}


def _locate(entries: list[dict[str, Any]], row: dict[str, Any]) -> dict[str, Any] | None:
    """The entry a row asked about, in the agenda as it stands: the n-th with its text."""
    want, n = row["entry"]["text"].rstrip(), row["entry"].get("n", 0)
    hits = [e for e in entries if e["text"].rstrip() == want]
    return hits[n] if n < len(hits) else None


def meeting_operations(dest: Any, state: Any, rows_: list[dict[str, Any]], answers: dict[str, Any]) -> list[dict[str, Any]]:
    """One roll operation per meeting from the answers to its rows, previewed by ``dest``."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows_:
        groups.setdefault((r["meeting"]["forum"], r["meeting"]["date"]), []).append(r)
    files = state.data.get("files") or {}
    out: list[dict[str, Any]] = []
    for (fid, date), group in groups.items():
        entries = dest.entries(files.get(group[0]["meeting"]["path"]))
        covered: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for r in group:
            opt = (answers.get(question_id(r)) or {}).get("option")
            e = _locate(entries, r)
            if e is None:
                continue  # edited since it was asked: it stays where it is
            if opt == "covered":
                covered.append(e)
            elif opt == "drop":
                dropped.append(e)
        if not covered and not dropped:
            continue
        fields = {"forum": {"id": fid}, "date": date, "lines": [ln for e in covered for ln in e["block"]],
                  "remove": [e["line"] for e in covered], "drop": [e["line"] for e in dropped],
                  "suffix": group[0]["suffix"]}
        action = Action(kind="agenda.roll", destination=dest.id, fields=fields, question_id=question_id(group[0]))
        out += [op.dump() for op in dest.preview(action, state)]
    return out


def plan(rc: "RunContext", decide: dict[str, Any]) -> dict[str, Any]:
    """What a plan node takes from covered answers: one roll per meeting, and an outcome for
    each kept entry (the seal remembers it)."""
    from docket.destinations import for_run
    rows_ = [r for r in decide.get("covered") or [] if r.get("question_id") in (decide.get("answers") or {})]
    answers = decide.get("answers") or {}
    ops: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    by_dest: dict[str, list[dict[str, Any]]] = {}
    for r in rows_:
        by_dest.setdefault(r["destination"], []).append(r)
        if answers[r["question_id"]].get("option") == "keep":
            m = r["meeting"]
            outcomes.append({"question": r["question_id"], "item": None, "topic": r["question_id"], "decision": "keep",
                             "note": f"not covered with {m['forum_name']} on {m['date']}: {context(r)['entry']['text']}",
                             "until": None})
    for did, group in sorted(by_dest.items()):
        if did not in rc.ws.config.destinations:
            continue
        dest = for_run(rc, did)
        try:
            ops += meeting_operations(dest, dest.snapshot(), group, answers)
        finally:
            dest.close()
    return {"operations": ops, "outcomes": outcomes}


__all__ = ["rows", "question_id", "question", "candidates", "meeting_operations", "plan", "LABELS", "OPTIONS"]
