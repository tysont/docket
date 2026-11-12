"""Options (TD §10.2) and forgiving option resolution (TD §10.3, AE-09).

An option is a typed action proposal: the action, its destination, and the
defaults Docket derived for it (due date, estimate, area, forum). Labels are
templates rendered against the item and those defaults.

``when`` guards are a closed set: ``has_forum``, ``has_reply_channel``,
``has_deadline``, ``has_calendar``, ``source_is:<a,b>`` and
``relation_is:<a,b>``. A guard that fails hides the option. An option whose
action needs a destination is then put to the destination adapter's
``can_accept``; a refusal removes the option and adds its reason to the
question (FR-DEC-03).

Resolution: an answer may name an option by id, label, the label's leading
words, its 1-based position, its destination or its action, case-insensitively.
Exactly one match wins; several raise ``input.ambiguous_option`` with the
candidates; none raises ``input.unknown_option``.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from docket.cli.errors import DocketError, InputError
from docket.config.models import ACTION_NEEDS_DESTINATION, WHEN_GUARDS
from docket.shapes.unexpected import unexpected
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

CALENDAR_ADAPTERS = ("calendar_file", "calendar_exec", "calendar_mcp")
DEFAULT_ADAPTER = {"task.add": ("markdown_tasks",), "agenda.add": ("markdown_agenda",),
                   "calendar.place": CALENDAR_ADAPTERS, "message.draft": ("message_mcp",)}


# --- resolution ----------------------------------------------------------------------


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(s).lower()).split())


def resolve_option(text: str, options: list[dict[str, Any]], *, question_id: str = "") -> dict[str, Any]:
    raw = str(text).strip()
    q = _norm(raw)
    cands = [{"id": o["id"], "label": o.get("label", "")} for o in options]
    if not q:
        raise InputError("input.unknown_option", "Empty answer.", details={"options": cands})
    if raw.isdigit():
        pos = int(raw)
        if 1 <= pos <= len(options):
            return options[pos - 1]
    low = raw.lower()
    tiers = [
        [o for o in options if o["id"].lower() == low],
        [o for o in options if _norm(o.get("label", "")) == q],
        [o for o in options if o.get("destination") and str(o["destination"]).lower() == low],
        [o for o in options if o.get("action") and str(o["action"]).lower() == low],
        [o for o in options if _norm(o.get("label", "")).startswith(q + " ") or _norm(o["id"]).startswith(q)],
        [o for o in options if _norm(o.get("label", "")).startswith(q)],
    ]
    for matches in tiers:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InputError(
                "input.ambiguous_option",
                f"{raw!r} matches {len(matches)} options: " + ", ".join(m["id"] for m in matches),
                hint=f"docket ask show {question_id}".strip(),
                details={"candidates": [{"id": m["id"], "label": m.get("label", "")} for m in matches]},
            )
    raise InputError(
        "input.unknown_option", f"{raw!r} names no option" + (f" of {question_id}" if question_id else ""),
        hint=f"docket ask show {question_id}".strip(), details={"options": cands},
    )


# --- item views and derived defaults ---------------------------------------------------------


def run_day(rc: "RunContext") -> date:
    return date.fromisoformat(rc.run.header.clock.date)


def forum_row(rc: "RunContext", forum_id: str | None) -> dict[str, Any] | None:
    for f in rc.ws.config.profile.forums:
        if f.id == forum_id:
            return {"id": f.id, "name": f.name or f.id}
    return None


def forum_for(rc: "RunContext", it: dict[str, Any]) -> dict[str, Any] | None:
    """The forum an item belongs to: where it would be raised. A meeting with the person who
    owns it, else with the person who sent it; else the forum of a group the sender is in,
    of a mentioned group, or of the topic it is about."""
    prof = rc.ws.config.profile
    sender = (it.get("sender") or {}).get("person")
    mentioned = set((it.get("mentioned") or {}).get("groups") or [])
    a = it.get("assessment") if isinstance(it.get("assessment"), dict) else {}
    with_person = {f.person: f.id for f in prof.forums if f.person}
    for who in (a.get("owner"), sender):
        if who and who in with_person:
            return forum_row(rc, with_person[who])
    for g in prof.groups:
        if g.forum and sender and sender in g.members:
            return forum_row(rc, g.forum)
    for g in prof.groups:
        if g.forum and g.id in mentioned:
            return forum_row(rc, g.forum)
    topics = {t.id: t for t in prof.topics}
    sig = it.get("signals") or {}
    for tid in [sig.get("topic"), a.get("topic"), *((it.get("mentioned") or {}).get("topics") or [])]:
        if tid in topics and topics[tid].forum:
            return forum_row(rc, topics[tid].forum)
    return None


def _unit_name(rc: "RunContext", it: dict[str, Any]) -> str:
    try:
        row = rc.run.store.db.execute("SELECT name FROM units WHERE source=? AND unit_id=?",
                                      (it.get("source"), it.get("unit"))).fetchone()
    except Exception:  # no registry (a context without a run): the unit says nothing
        return ""
    return str(row["name"] or "") if row is not None else ""


def area_for(rc: "RunContext", it: dict[str, Any]) -> str:
    """The item's area: a rule's word, else the area of the topic its unit is named after
    (an owned one first), else the area its own text points to when it points one way."""
    sig = it.get("signals") or {}
    if sig.get("area"):
        return str(sig["area"])
    topics = {t.id: t for t in rc.ws.config.profile.topics}
    name = _unit_name(rc, it)
    if name:
        from docket.items.identity import ProfileIndex
        named = [topics[t] for t in ProfileIndex(rc.ws.config.profile).mentions(name)["topics"] if t in topics]
        for t in sorted(named, key=lambda t: (not t.owned, t.id)):
            if t.area:
                return str(t.area)
    areas = []
    for tid in [sig.get("topic"), *((it.get("mentioned") or {}).get("topics") or []),
                (it.get("assessment") or {}).get("topic")]:
        if tid in topics and topics[tid].area and str(topics[tid].area) not in areas:
            areas.append(str(topics[tid].area))
    return areas[0] if len(areas) == 1 else ""


def deadline_of(it: dict[str, Any]) -> str | None:
    dl = (it.get("signals") or {}).get("deadline") or {}
    if isinstance(dl, dict) and dl.get("date"):
        return str(dl["date"])[:10]
    a = (it.get("assessment") or {}).get("deadline")
    return str(a)[:10] if a else None


def _est_of(it: dict[str, Any]) -> int | None:
    from docket.text.durations import parse_duration
    a = it.get("assessment") or {}
    for k in ("est", "estimate"):
        if a.get(k) not in (None, ""):
            try:
                return parse_duration(a[k])
            except DocketError:
                unexpected("decide.defaults", "assessment estimate is not a duration", sample=str(a[k])[:40],
                           reading="use the default estimate", extend="tasks.assess fields: est")
    return None


def derived_defaults(rc: "RunContext", it: dict[str, Any]) -> dict[str, Any]:
    """Due, estimate, area and forum an option proposes before any override (FR-DEC-02)."""
    from docket.text.durations import parse_duration
    d = rc.ws.config.decisions.defaults
    pol = rc.ws.config.planning.policy
    deadline = deadline_of(it)
    lead = d.lead_days if d.lead_days is not None else pol.default_lead_days
    if deadline:
        due, due_source = deadline, "deadline"
    else:
        from docket.decide.operations import workdays
        from docket.text.dates import add_workdays
        due, due_source = add_workdays(run_day(rc), lead, workdays(rc)).isoformat(), "lead_days"
    est = _est_of(it)
    est_source = "assessment"
    if est is None:
        est, est_source = parse_duration(d.est if d.est is not None else pol.default_est), "default"
    forum = forum_for(rc, it)
    return {"due": due, "due_source": due_source, "est": est, "est_source": est_source,
            "area": area_for(rc, it), "forum": forum["id"] if forum else None}


def item_view(rc: "RunContext", it: dict[str, Any]) -> dict[str, Any]:
    """The template context of an item for prompts and labels."""
    a = it.get("assessment") or {}
    forum = forum_for(rc, it)
    src_cfg = rc.ws.config.sources.get(it.get("source")) if it.get("source") else None
    return {
        "source_label": (getattr(src_cfg, "label", None) or it.get("source") or ""),
        "key": _item_key(it),
        "item_id": it.get("id"), "source": it.get("source"), "unit": it.get("unit"), "title": it.get("title"),
        "text": it.get("text"), "from": it.get("from"), "at": it.get("at"), "url": it.get("url"),
        "score": it.get("score"), "fields": it.get("fields") or {}, "forum": forum or {},
        "area": area_for(rc, it), "topic": it.get("topic") or "", "ask": a.get("ask") or "",
        "owner": a.get("owner") or "", "kind": a.get("kind") or "", "deadline": deadline_of(it) or "",
        "quote": _first_quote(a),
        # words the workspace asked the assessment for beyond the core fields (tasks.assess.extra)
        "extra": {k: str(a.get(k) or "") for k in _extra_names(rc)},
    }


def _extra_names(rc: "RunContext") -> list[str]:
    tasks = getattr(rc.ws.config, "tasks", None)
    return sorted(tasks.assess.extra) if tasks is not None else []


def _item_key(it: dict[str, Any]) -> str:
    """The item's ledger key (TD §13.8): what a destination writes so the item is recognised later."""
    from docket.items.ledger import item_key
    topic = it.get("topic") or it.get("id")
    return item_key(str(topic)) if topic else ""


def _first_quote(assessment: dict[str, Any]) -> str:
    """The first words the assess task quoted as evidence: what made the item a finding."""
    for e in assessment.get("evidence") or []:
        q = e.get("quote") if isinstance(e, dict) else e
        if isinstance(q, str) and q.strip():
            return q.strip()
    return ""


def recommended_option(it: dict[str, Any], offered: list[dict[str, Any]], recommend: Any) -> str | None:
    """The option a question recommends for its item: the decision the assess task chose when
    it is on offer; for an item the task did not keep and a rule surfaced, the configured
    ``surfaced`` option; else the configured ``default``. None when none of those is on offer."""
    ids = {o["id"] for o in offered}
    cfg = recommend if isinstance(recommend, dict) else (recommend.model_dump() if recommend is not None else {})
    a = it.get("assessment") or {}
    surfaced = bool((it.get("signals") or {}).get("surface")) and a.get("keep") is False
    for pick in ([cfg.get("surfaced")] if surfaced else [a.get("decision")]) + [cfg.get("default")]:
        if pick and pick in ids:
            return str(pick)
    return None


# --- guards ------------------------------------------------------------------------------------


def has_calendar(rc: "RunContext") -> bool:
    from docket.plan.build import planning_active
    if planning_active(rc):
        return True
    return any(d.adapter in CALENDAR_ADAPTERS for d in rc.ws.config.destinations.values())


def guard_ok(rc: "RunContext", guard: str | None, it: dict[str, Any], defaults: dict[str, Any]) -> bool:
    if not guard:
        return True
    name, _, arg = guard.partition(":")
    vals = {v.strip().lower() for v in arg.split(",") if v.strip()}
    sender = it.get("sender") or {}
    if name == "has_forum":
        return defaults.get("forum") is not None
    if name == "has_reply_channel":
        return bool(sender.get("handle")) and not sender.get("is_me")
    if name == "has_deadline":
        return defaults.get("due_source") == "deadline"
    if name == "has_calendar":
        return has_calendar(rc)
    if name == "source_is":
        return str(it.get("source") or "").lower() in vals
    if name == "relation_is":
        rels = {str(r).lower() for r in [sender.get("relation"), *(sender.get("relations") or [])] if r}
        return bool(rels & vals)
    unexpected("decide.options", f"unknown option guard {name}", sample=guard,
               reading="hide the option", extend=f"one of {', '.join(WHEN_GUARDS)}")
    return False


# --- destinations and can_accept ---------------------------------------------------------------


def default_destination(rc: "RunContext", action: str, named: str | None) -> str | None:
    if named:
        return named
    dests = rc.ws.config.destinations
    if action == "calendar.place":
        cal = rc.ws.config.planning.calendar
        if cal is not None and cal.destination in dests:
            return cal.destination
    if action == "task.add":
        pt = rc.ws.config.planning.tasks.destination
        if pt and pt in dests and dests[pt].adapter == "markdown_tasks":
            return pt
    want = DEFAULT_ADAPTER.get(action, ())
    for name in sorted(dests):
        if dests[name].adapter in want:
            return name
    return None


class Acceptor:
    """Asks destination adapters ``can_accept``, once per (destination, action, target) per evaluation."""

    def __init__(self, rc: "RunContext") -> None:
        self.rc = rc
        self.adapters: dict[str, Any] = {}
        self.cache: dict[tuple[Any, ...], tuple[bool, str | None]] = {}

    def adapter(self, dest_id: str) -> Any:
        if dest_id not in self.adapters:
            from docket.destinations import build, env_for, for_run
            cfg = self.rc.ws.config.destinations[dest_id]
            if cfg.adapter.startswith("markdown"):
                self.adapters[dest_id] = for_run(self.rc, dest_id)
            else:  # remote adapters: the live object answers capability questions without I/O
                self.adapters[dest_id] = build(dest_id, cfg, env_for(self.rc.ws, run=self.rc.run, env=self.rc.env))
        return self.adapters[dest_id]

    def check(self, action: str, dest_id: str | None, fields: dict[str, Any]) -> tuple[bool, str | None]:
        from docket.destinations.base import Action
        rc = self.rc
        if action == "calendar.place":
            from docket.plan.build import planning_active
            if not planning_active(rc):
                return False, "calendar blocks need planning.calendar and a plan node with schedule_tasks"
            if dest_id is None:
                return True, None  # the scheduler places the block; apply needs a calendar adapter (TD §12)
        if dest_id is None:
            return False, f"no configured destination accepts {action}"
        if dest_id not in rc.ws.config.destinations:
            return False, f"destination {dest_id!r} is not configured"
        forum = fields.get("forum") if isinstance(fields.get("forum"), dict) else {}
        key = (dest_id, action, (forum or {}).get("id"), fields.get("agenda"))
        if key not in self.cache:
            try:
                ad = self.adapter(dest_id)
                if ad.adapter.endswith("_mcp") and getattr(ad, "denv", None) is not None and ad.denv.call_tool is None:
                    self.cache[key] = (False, f"{dest_id}: no MCP transport is configured for this destination")
                    return self.cache[key]
                acc = ad.can_accept(Action(kind=action, destination=dest_id, fields=fields))
                self.cache[key] = (acc.ok, acc.reason)
            except DocketError as e:
                self.cache[key] = (False, f"{dest_id}: {e.message}")
        return self.cache[key]

    def close(self) -> None:
        for a in self.adapters.values():
            try:
                a.close()
            except Exception:  # closing is best effort
                pass
        self.adapters.clear()


def label_context(view: dict[str, Any], defaults: dict[str, Any], forum: dict[str, Any] | None) -> dict[str, Any]:
    return {**view, "due": defaults.get("due") or "", "est": defaults.get("est") or "", "when": "",
            "forum": forum or view.get("forum") or {}}


def finding_options(rc: "RunContext", it: dict[str, Any], opts: list[dict[str, Any]], acceptor: Acceptor,
                    view: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, str]],
                                                                 dict[str, Any]]:
    """(offered options, refused [{option, reason}], derived defaults) for one finding."""
    view = view or item_view(rc, it)
    defaults = derived_defaults(rc, it)
    forum = forum_row(rc, defaults.get("forum"))
    ctx = label_context(view, defaults, forum)
    offered, refused = [], []
    for o in opts:
        if not guard_ok(rc, o.get("when"), it, defaults):
            continue
        action = o["action"]
        dest = default_destination(rc, action, o.get("destination")) if action in ACTION_NEEDS_DESTINATION else None
        if action == "agenda.add" and not o.get("destination") and forum:
            from docket.decide.operations import forum_agenda_destination
            dest = forum_agenda_destination(rc, forum["id"]) or dest
        # An agenda item with no forum derived for it is still offered: the answer names the forum.
        needs = ["forum"] if action == "agenda.add" and not forum else []
        if action in ACTION_NEEDS_DESTINATION and not needs:
            ok, reason = acceptor.check(action, dest, ctx)
            if not ok:
                refused.append({"option": o["id"], "action": action, "reason": reason or "not accepted"})
                continue
        od = {k: v for k, v in defaults.items() if k in ("due", "est", "area", "forum")}
        offered.append({"id": o["id"], "label": templates.render(o["label"], ctx), "action": action,
                        "destination": dest, "defaults": od, **({"set": dict(o["set"])} if o.get("set") else {}),
                        **({"batch": True} if o.get("batch") else {}), **({"needs": needs} if needs else {})})
    return offered, refused, defaults


__all__ = ["resolve_option", "finding_options", "derived_defaults", "item_view", "Acceptor", "default_destination",
           "forum_row", "guard_ok", "has_calendar"]
