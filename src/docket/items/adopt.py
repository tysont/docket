"""Adopting task lines that carry no key (TD §11.5, §12.3, §13.8).

A task list is also edited by hand and by other tools, which add open lines in its grammar
without the ledger key. A ``markdown_tasks`` destination with ``adopt: true`` gives each such
line its key, so the ledger follows it like a line Docket wrote.

Which lines: the open task lines of the destination's own sections (``section`` and
``sections``) whose key field is absent or empty. A closed line, a line in a section the
destination does not own, and a line that carries a key are left alone.

The item: its topic is the one ``docket item add`` derives (``docket.items.add.topic_of``)
from the line's words (the text without links or fields, ``TaskLine.words``) and its first
link, so a line typed by hand and the same task added with ``item add`` are one item.

The line: the same line, its words, links and fields as written, with the key and every
other field ``quality.require`` names that is missing, has a default and is not in
``quality.optional`` appended in the order ``quality.require`` gives, under the names
``quality.fields`` gives. No imperative rewrite and no other quality rule runs: the line is
the operator's own, so a missing link or evidence is not a refusal.

Who adopts: one plan node per graph (``node``), so two plan nodes never adopt the same
line. It is the first plan node an approve node approves and an apply node writes that
sets ``schedule_tasks``, else the first such plan node. The adoption is a ``replace``
operation in its plan, approved and applied like any other, and the seal enters the item
in the ledger once the line is written (``enter``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable

from docket.config.defaults import DEFAULT_SECTION
from docket.items import ledger
from docket.items.add import SOURCE, topic_of

if TYPE_CHECKING:
    from docket.engine.context import RunContext

KEY = "key"
CLOSED = ("done", "ignored")


@dataclass(frozen=True)
class Adoption:
    """One keyless open line and what it becomes. ``was`` is the ledger state of the row the
    topic already has, or None."""
    destination: str
    path: str
    anchor: str
    old_line: str
    line: str
    key: str
    topic: str
    title: str
    url: str | None
    due: str | None
    est: int | None
    area: str | None
    was: str | None = None

    def op(self) -> dict[str, Any]:
        """The plan operation: a ``replace`` of the line, with what the ledger needs under ``adopt``."""
        from docket.destinations.base import make_op
        from docket.destinations.markdown_io import line_digest
        op = make_op(self.destination, "replace", ident=self.anchor, path=self.path, anchor=self.anchor,
                     old_digest=line_digest(self.old_line), old_line=self.old_line, line=self.line,
                     item_id=self.key).dump()
        op["adopt"] = {k: v for k, v in (("key", self.key), ("topic", self.topic), ("title", self.title),
                                         ("url", self.url), ("due", self.due), ("est", self.est),
                                         ("area", self.area), ("was", self.was)) if v is not None}
        return op


def adopts(cfg: Any) -> bool:
    return bool(getattr(cfg, "adopt", False)) and cfg.adapter == "markdown_tasks"


def destinations(config: Any) -> list[str]:
    """The destinations that adopt task lines, by name."""
    return [d for d in sorted(config.destinations) if adopts(config.destinations[d])]


def owned_sections(cfg: Any) -> list[str]:
    """The headings the destination writes: its ``section`` (or the default) and its ``sections``."""
    return list(dict.fromkeys([cfg.section or DEFAULT_SECTION, *cfg.sections.values()]))


def field_name(cfg: Any, name: str) -> str:
    """The name a required attribute has on the destination's lines (``quality.fields``)."""
    return ((cfg.quality.fields if cfg.quality is not None else {}) or {}).get(name, name)


def node(rc: "RunContext") -> str | None:
    """The one plan node of the run's graph that adopts, or None."""
    if not destinations(rc.ws.config):
        return None
    applied = {rc.graph[n].apply for n in rc.by_verb("apply")}
    approved = {rc.graph[n].approve for n in rc.by_verb("approve") if n in applied}
    plans = [n for n in rc.by_verb("plan") if n in approved]
    placing = [n for n in plans if rc.graph[n].schedule_tasks]
    return (placing or plans or [None])[0]


def adopted_line(dest: Any, line: str, key: str, *, today: str, workdays: Iterable[int]) -> str:
    """``line`` with its key and the missing required fields that have a default."""
    from docket.destinations.quality import default_for
    q = dest.cfg.quality
    if q is None:   # validation refuses adopt without quality rules that require the key
        return line
    run = {"date": today, "workdays": sorted(workdays)}
    new = line
    for name in q.require:
        if name == "link" or (name != KEY and name in q.optional):
            continue
        on_line = field_name(dest.cfg, name)
        t = dest.grammar.parse(new)
        if t is None or t.get(on_line) not in (None, ""):
            continue
        value = key if name == KEY else default_for(q, name, run)
        if value is not None:
            new = dest.grammar.set_field(new, on_line, value)
    return new


def _due(v: str | None, today: str) -> str | None:
    from datetime import date

    from docket.cli.errors import InputError
    from docket.text.dates import parse_date
    if not v:
        return None
    try:
        return parse_date(v, date.fromisoformat(today)).isoformat()
    except (InputError, ValueError):
        return None


def _est(v: str | None) -> int | None:
    from docket.cli.errors import InputError
    from docket.text.durations import parse_duration
    if not v:
        return None
    try:
        return parse_duration(v)
    except (InputError, ValueError):
        return None


def scan(dest: Any, state: Any, *, today: str, workdays: Iterable[int],
         lookup: Callable[[str], dict[str, Any] | None] | None = None) -> list[Adoption]:
    """The adoptions a ``markdown_tasks`` destination's snapshot calls for, in file order.
    ``lookup`` reads the ledger row of a key."""
    cfg = dest.cfg
    if state.data.get("text") is None:
        return []
    kf = field_name(cfg, KEY)
    out: list[Adoption] = []
    seen: set[tuple[str, str]] = set()
    for o in dest.open_tasks(state, sections=owned_sections(cfg)):
        if str(o.fields.get(kf) or "").strip() or (o.section, o.line) in seen:
            continue
        t = dest.grammar.parse(o.line)
        if t is None:
            continue
        words, links = t.words, t.links
        if not words and not links:
            continue  # an empty checkbox is no task
        seen.add((o.section, o.line))
        link = links[0] if links else None
        topic = topic_of(words, link)
        key = ledger.item_key(topic)
        new = adopted_line(dest, o.line, key, today=today, workdays=workdays)
        nt = dest.grammar.parse(new)
        fields = dict(nt.fields) if nt is not None else {}
        row = lookup(key) if lookup is not None else None
        out.append(Adoption(destination=dest.id, path=dest.rel, anchor=o.section, old_line=o.line, line=new,
                            key=key, topic=topic, title=words or t.title, url=link,
                            due=_due(fields.get(field_name(cfg, "due")), today),
                            est=_est(fields.get(field_name(cfg, "est"))),
                            area=(fields.get(field_name(cfg, "area")) or "").strip() or None,
                            was=row["state"] if row else None))
    return out


def _workdays(ws: Any) -> frozenset[int]:
    from docket.plan.types import Policy
    return Policy.from_config(ws.config.planning, ws.config.profile.workdays).workdays


def for_plan(rc: "RunContext", name: str | None = None) -> list[Adoption]:
    """The adoptions of the run's adopting plan node (``name``, when given, must be it): every
    adopting destination read as the run reads it, its sandbox twin in a sandbox run."""
    from docket.destinations import for_run
    from docket.shapes.unexpected import collecting
    who = node(rc)
    if who is None or (name is not None and name != who):
        return []
    db = rc.run.store.db
    today = rc.run.header.clock.date
    workdays = _workdays(rc.ws)
    out: list[Adoption] = []
    with collecting():  # a line the grammar cannot parse is recorded by the plan's own read of the store
        for did in destinations(rc.ws.config):
            dest = for_run(rc, did)
            try:
                out += scan(dest, dest.snapshot(), today=today, workdays=workdays,
                            lookup=lambda key: ledger.get(db, key))
            finally:
                dest.close()
    return out


def pending(ws: Any, db: Any, *, today: str, env: Any = None) -> list[Adoption]:
    """The adoptions the live destinations call for now, with no run (``docket ledger reconcile``)."""
    from docket.destinations import build, env_for
    from docket.shapes.unexpected import collecting
    out: list[Adoption] = []
    with collecting():
        for did in destinations(ws.config):
            dest = build(did, ws.config.destinations[did], env_for(ws, env=env))
            try:
                out += scan(dest, dest.snapshot(), today=today, workdays=_workdays(ws),
                            lookup=lambda key: ledger.get(db, key))
            finally:
                dest.close()
    return out


def describe(op: dict[str, Any]) -> str:
    """An adoption in the plan diff: which line, which key, what it becomes."""
    a = op.get("adopt") or {}
    where = f"{op.get('path', '')} under {op.get('anchor', '')}".strip()
    text = f"adopt `{op.get('old_line', '')}` as {a.get('key')}: `{op.get('line')}` ({where})"
    if a.get("was") in CLOSED:
        text += f"; reopens {a.get('key')}, which the ledger has as {a['was']}"
    elif a.get("was"):
        text += f"; the ledger has {a.get('key')} ({a['was']})"
    return text


def enter(db: Any, ops: list[dict[str, Any]], receipts: dict[str, dict[str, Any]], *, run_id: str,
          at: str) -> list[dict[str, Any]]:
    """Enter the items of the adoptions that were written (the caller holds the transaction).

    Each is in ``backlog`` with the line as its placement and the due date, estimate and area
    the line carries, through the path that records an item added by hand. A row the topic
    already has keeps its history and gains the placement. One that was done or ignored is
    reopened: the open line says the work is not finished, and a ledger that called it done
    would lose the line. Returns the items entered.
    """
    moves = []
    for op in ops:
        a = op.get("adopt")
        rec = receipts.get(op.get("op_id", "")) or {}
        if not a or rec.get("outcome") not in ("applied", "already_present"):
            continue
        old = ledger.get(db, a["key"])
        did = op["destination"]
        note = f"adopted from {did}"
        if old is not None and old["state"] in CLOSED:
            note = f"an open line was found in {did}; reopened from {old['state']}"
        placement = {k: v for k, v in (("destination", did), ("adapter", "markdown_tasks"), ("op_id", op["op_id"]),
                                       ("path", op.get("path")), ("anchor", op.get("anchor")),
                                       ("line", op.get("line")), ("run", run_id)) if v not in (None, "")}
        row = ledger.added(db, {"key": a["key"], "topic": a["topic"], "source": SOURCE, "title": a.get("title"),
                                "url": a.get("url"), "due": a.get("due"), "est": a.get("est"), "area": a.get("area"),
                                "placement": placement}, run_id=run_id, at=at, note=note, decision="adopted", by=None)
        ledger.put(db, row)
        moves.append({"key": a["key"], "title": row["title"], "was": old["state"] if old else None,
                      "state": row["state"], "note": note})
    return moves


__all__ = ["Adoption", "adopts", "destinations", "owned_sections", "node", "adopted_line", "scan", "for_plan",
           "pending", "describe", "enter"]
