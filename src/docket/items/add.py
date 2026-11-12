"""A task added by hand (TD §13.8): ``docket item add``.

The operator decided already, so no question is asked. One command writes the task line
to a task store and enters the item in the ledger, outside any run. The line is the
destination's own: the ``task.add`` action goes through ``preview`` (line template and
quality rules), then ``apply`` and ``verify``, as a triaged item's does. Its fields have
the shape ``docket.decide.operations.finding_context`` gives a finding, so a workspace's
line template renders a hand-added task the way it renders any other: the words are the
``action``, the first link is the ``url``, and there is no sender and no quote.

The item's topic is its words and its first link, so adding the same task again finds
the first one. Nobody approves the write, so only a private destination takes it, the
rule a ``write`` node follows.
"""
from __future__ import annotations

from typing import Any, Callable

from docket.cli.errors import ExternalError, InputError, StateError
from docket.config.digest import digest
from docket.items import ledger

SOURCE = "hand"
ACTION = "task.add"


def topic_of(text: str, link: str | None) -> str:
    """The topic of a hand-added item: its words, spaced and cased loosely, and its first link."""
    words = " ".join(str(text).split()).casefold()
    return "hand:" + digest([words, (link or "").strip()])[:16]


def key_of(text: str, link: str | None) -> str:
    return ledger.item_key(topic_of(text, link))


def task_stores(ws: Any) -> list[str]:
    """The destinations whose adapter accepts ``task.add``, by name."""
    from docket.destinations import ADAPTERS, adapter_class
    out = []
    for did in sorted(ws.config.destinations):
        adapter = ws.config.destinations[did].adapter
        if adapter in ADAPTERS and ACTION in getattr(adapter_class(adapter), "capabilities", ()):
            out.append(did)
    return out


def destination(ws: Any, to: str | None) -> str:
    """The task store a hand-added task goes to: ``to``, else the only one there is."""
    stores = task_stores(ws)
    if to is not None:
        if to in stores:
            return to
        from docket.config.loader import closest
        what = "is not a destination that accepts task.add" if to in ws.config.destinations else "is not a destination"
        raise InputError("input.unknown_destination", f"{to!r} {what}. Task stores: {', '.join(stores) or 'none'}.",
                         hint="docket config explain destinations",
                         details={"destinations": stores, "suggestion": closest(to, stores)})
    if not stores:
        raise InputError("input.unknown_destination", "The workspace has no destination that accepts task.add.",
                         hint="docket config explain destinations", details={"destinations": []})
    if len(stores) > 1:
        raise InputError("input.ambiguous_destination",
                         f"Several destinations accept task.add: {', '.join(stores)}. Name one with --to.",
                         hint=f"docket item add <text> --to {stores[0]}", details={"destinations": stores})
    return stores[0]


def action_fields(ws: Any, *, text: str, links: list[str], label: str, due: str | None, est: int | None,
                  area: str | None, tier: str | None, note: str | None, today: str, key: str,
                  topic: str) -> dict[str, Any]:
    """The rendering context of the ``task.add`` action: the fields a finding's action carries."""
    from docket.plan.types import Policy
    workdays = Policy.from_config(ws.config.planning, ws.config.profile.workdays).workdays
    tasks = getattr(ws.config, "tasks", None)
    extra = sorted(tasks.assess.extra) if tasks is not None else []
    ctx: dict[str, Any] = {
        "source_label": label, "key": key, "item_id": key, "source": SOURCE, "unit": "", "title": text,
        "text": "", "from": "", "at": "", "url": links[0] if links else "", "score": "", "fields": {}, "forum": {},
        "area": area or "", "topic": topic, "ask": "", "owner": "", "kind": "", "deadline": "", "quote": "",
        "extra": {k: "" for k in extra},
        "question_id": "", "action": text, "due": due or "", "est": est if est is not None else "", "when": "",
        "note": note or "", "links": list(links),
        "run": {"id": f"add-{today}", "date": today, "workdays": sorted(workdays)},
    }
    if tier:
        ctx["tier"] = tier  # as an option's ``set: {tier: ...}`` does
    return ctx


def _open_line(dest: Any, key: str, lines: list[str]) -> Any:
    """The open task in ``dest`` that carries ``key`` in a field or is one of ``lines`` as written."""
    if not hasattr(dest, "open_tasks"):
        return None
    want = {ln.rstrip() for ln in lines if ln}
    for t in dest.open_tasks(dest.snapshot()):
        if any(str(v).strip() == key for v in t.fields.values()) or t.line.rstrip() in want:
            return t
    return None


def add(ws: Any, db: Any, *, text: str, links: list[str], label: str, due: str | None, est: int | None,
        area: str | None, tier: str | None, note: str | None, to: str | None, today: str, at: str,
        env: Any = None, fix: Callable[[str, str], str | None] | None = None, write: bool = True) -> dict[str, Any]:
    """Write one task line and its ledger row (the caller commits). Returns ``key``, ``line``,
    ``destination``, ``path``, ``section``, ``changed`` and the ledger row (``ledger``).

    An item still open in a task store is not written again (``changed`` false). One that
    is done or ignored refuses with ``item.closed``. With ``write`` false nothing is written.
    """
    from docket.destinations import build, env_for
    from docket.destinations.base import Action
    text = " ".join(str(text).split())
    if not text:
        raise InputError("input.bad_argument", "The task needs words: say what to do.",
                         hint="docket item add 'Call the dentist'")
    did = destination(ws, to)
    cfg = ws.config.destinations[did]
    if cfg.visibility != "private":
        raise InputError("item.shared_destination",
                         f"{did} is a shared destination. A task added by hand is approved by nobody, so it goes "
                         f"only to a private destination.",
                         hint="docket item add <text> --to <private destination>",
                         details={"destination": did, "private": [d for d in task_stores(ws)
                                                                  if ws.config.destinations[d].visibility == "private"]})
    if tier is not None and tier not in cfg.sections:
        tiers = sorted(cfg.sections)
        from docket.config.loader import closest
        raise InputError("input.unknown_tier",
                         f"{did} has no tier {tier!r}. Tiers: {', '.join(tiers) or 'none configured'}.",
                         hint=f"docket config explain destinations.{did}.sections",
                         details={"destination": did, "tiers": tiers, "suggestion": closest(tier, tiers)})
    link = links[0] if links else None
    topic = topic_of(text, link)
    key = ledger.item_key(topic)
    row = ledger.get(db, key)
    if row is not None and row["state"] in ("done", "ignored"):
        raise StateError("item.closed",
                         f"{key} is already in the ledger and {row['state']}. Reopen it with docket ledger edit.",
                         hint=f"docket ledger edit {key} --state backlog",
                         details={"key": key, "state": row["state"], "title": row["title"]})
    stores = set(task_stores(ws))
    dest = build(did, cfg, env_for(ws, env=env))
    try:
        if row is not None:   # still open in the ledger: is its line still open where it was put?
            for p in reversed(row["placements"]):
                pd = p.get("destination")
                if pd not in stores or pd == did:
                    continue
                other = build(pd, ws.config.destinations[pd], env_for(ws, env=env))
                try:
                    found = _open_line(other, key, [str(p.get("line") or "")])
                finally:
                    other.close()
                if found is not None:
                    return _result(key, found.line, pd, other.rel, found.section or p.get("anchor"), False, row, write)
            lines = [str(p.get("line") or "") for p in row["placements"] if p.get("destination") == did]
            found = _open_line(dest, key, lines)
            if found is not None:
                return _result(key, found.line, did, dest.rel, found.section, False, row, write)
        ctx = action_fields(ws, text=text, links=links, label=label, due=due, est=est, area=area, tier=tier,
                            note=note, today=today, key=key, topic=topic)
        action = Action(kind=ACTION, destination=did, fields=ctx, key=key, item_id=key)
        state = dest.snapshot()
        ops = dest.preview(action, state, fix=fix) if cfg.adapter == "markdown_tasks" else dest.preview(action, state)
        op = ops[0]
        run_id = f"add-{today}"
        placement = {k: v for k, v in (("destination", did), ("adapter", cfg.adapter), ("op_id", op.op_id),
                                       ("path", op.path), ("anchor", op.anchor), ("line", op.line),
                                       ("run", run_id)) if v not in (None, "")}
        new = ledger.added(db, {"key": key, "topic": topic, "source": SOURCE, "title": text, "url": link,
                                "due": due, "est": est, "area": area, "placement": placement},
                           run_id=run_id, at=at, note=note)
        outcome = None
        if write:
            rec = dest.apply(op)
            vr = dest.verify(op, rec)
            if not vr.ok:
                raise ExternalError("dest.apply_failed", f"{did}: the line was written but does not read back "
                                                         f"({vr.status}: {vr.detail}).",
                                    hint=f"docket item add {text!r} --to {did}",
                                    details={"destination": did, "op_id": op.op_id, "verify": vr.model_dump()})
            outcome = rec.outcome
            ledger.put(db, new)
    finally:
        dest.close()
    out = _result(key, op.line or "", did, op.path, op.anchor, True, new, write)
    out.update({"op_id": op.op_id, "outcome": outcome})
    return out


def _result(key: str, line: str, did: str, path: str | None, section: str | None, changed: bool,
            row: dict[str, Any], write: bool) -> dict[str, Any]:
    return {"key": key, "line": line, "destination": did, "path": path, "section": section, "changed": changed,
            "dry_run": not write, "ledger": row}
