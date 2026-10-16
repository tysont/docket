"""Task verification (TD §10.5): open tasks that look done.

An open task whose line names a reference (a ``signals.references`` pattern or
a link) that a source claims through ``resolve`` (TD §6.10) is a candidate.
The resolution is read once, before the burst (FR-DEC-07): ``decide`` waits
with a ``run`` step for ``docket ask resolve``, which resolves every pending
reference and stores the results in the run's ``decide/resolutions.json``
keyed by reference and generation. A refresh opens a new generation and so
resolves again.

A task whose reference is in ``decisions.verify.closed_states``, or whose
last actor is the operator after the task's ``decisions.verify.date_field``,
becomes a ``verify`` question with ``close``, ``keep`` and ``update``. At most
``decisions.verify.max_per_run`` tasks are resolved per run.

Findings the review flagged ``needs_verification`` resolve their references
the same way; the result is shown with the finding.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from docket.config.digest import digest, pretty_json
from docket.decide.store import StoreTask, open_tasks
from docket.store.paths import read_json, write_atomic
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

LABELS = {"close": "Close it", "keep": "Keep it open", "update": "Update it (answer with --title, --due or --est)"}
PROMPT = "Looks done: close \"{task.title}\"? {reason}"
ACTIONS = {"close": "verify.close", "keep": "verify.keep", "update": "verify.update"}


def cache_path(rc: "RunContext") -> Any:
    return rc.run.dir / "decide" / "resolutions.json"


def read_cache(rc: "RunContext") -> dict[str, Any]:
    p = cache_path(rc)
    return read_json(p) if p.is_file() else {}


def generation(rc: "RunContext") -> int:
    from docket.sources.generations import current_generation
    return current_generation(rc)


def _patterns(rc: "RunContext") -> list[re.Pattern[str]]:
    refs = (rc.ws.config.signals or {}).get("references") or {}
    out = []
    for _, spec in sorted(refs.items()):
        pat = spec.get("pattern") if isinstance(spec, dict) else None
        if pat:
            try:
                out.append(re.compile(pat))
            except re.error:
                continue  # validation reports the pattern
    return out


def refs_in(rc: "RunContext", text: str, links: list[str]) -> list[str]:
    out: list[str] = []
    for r in _patterns(rc):
        for m in r.finditer(text):
            v = m.group("id") if "id" in r.groupindex else m.group(0)
            if v and v not in out:
                out.append(v)
    for u in links:
        if u not in out:
            out.append(u)
    return out


def _claimed(rc: "RunContext", refs: list[str]) -> str | None:
    from docket.sources.resolve import claimant
    for ref in refs:
        sid = claimant(rc.ws, ref)
        if sid is not None and rc.ws.config.sources[sid].transport:
            return ref
    return None


def task_refs(rc: "RunContext", t: StoreTask) -> str | None:
    from docket.text.grammar import Grammar
    g = Grammar.from_config(rc.ws.config.destinations[t.destination].grammar)
    parsed = g.parse(t.line)
    links = parsed.links if parsed is not None else []
    return _claimed(rc, refs_in(rc, t.line, links))


def item_ref(rc: "RunContext", it: dict[str, Any]) -> str | None:
    refs = [r.get("value") for r in it.get("references") or [] if r.get("value")]
    if it.get("url"):
        refs.append(it["url"])
    return _claimed(rc, [r for r in refs if r])


def targets(rc: "RunContext", items: list[dict[str, Any]], tasks: list[StoreTask] | None = None) -> dict[str, Any]:
    """Verification candidates: tasks (bounded) and flagged findings, each with its claimed reference."""
    if "verify" not in rc.workflow.questions.kinds:
        tasks_out: list[dict[str, Any]] = []
    else:
        tasks = open_tasks(rc) if tasks is None else tasks
        tasks_out = []
        cap = rc.ws.config.decisions.verify.max_per_run
        for t in sorted(tasks, key=lambda x: x.id):
            if len(tasks_out) >= cap:
                break
            ref = task_refs(rc, t)
            if ref is not None:
                tasks_out.append({"task": t.view(), "ref": ref})
    flagged = []
    for it in items:
        if it.get("needs_verification"):
            ref = item_ref(rc, it)
            if ref is not None:
                flagged.append({"item_id": it["id"], "ref": ref})
    return {"tasks": tasks_out, "items": flagged}


def pending(rc: "RunContext", tg: dict[str, Any]) -> list[str]:
    cache = read_cache(rc)
    gen = generation(rc)
    refs = sorted({r["ref"] for r in tg["tasks"] + tg["items"]})
    return [r for r in refs if (cache.get(r) or {}).get("generation") != gen]


def resolve_pending(rc: "RunContext", refs: list[str]) -> dict[str, Any]:
    from docket.sources.resolve import resolve_ref
    cache = read_cache(rc)
    gen = generation(rc)
    for ref in refs:
        r = resolve_ref(rc.ws, ref, env=rc.env, log_dir=rc.run.dir / "logs")
        cache[ref] = {"source": r.source, "status": r.status, "state": r.state, "last_actor": r.last_actor,
                      "last_activity": r.last_activity, "title": r.title, "cause": r.cause, "generation": gen}
    write_atomic(cache_path(rc), pretty_json(cache))
    return cache


def _is_me(rc: "RunContext", actor: str | None) -> bool:
    if not actor:
        return False
    me = rc.ws.config.profile.me
    names = {me.name, *me.aliases, *me.handles.values()}
    return actor.strip().casefold() in {n.casefold() for n in names if n}


def _date_of(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(v)[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", str(v)) else None


def looks_done(rc: "RunContext", res: dict[str, Any], task_date: str | None) -> str | None:
    """The reason a resolution says the work is done, or None."""
    if res.get("status") != "resolved":
        return None
    cfg = rc.ws.config.decisions.verify
    state = str(res.get("state") or "")
    if state and state.casefold() in {s.casefold() for s in cfg.closed_states}:
        return f"{res.get('source')} reports it {state}."
    if _is_me(rc, res.get("last_actor")):
        last = _date_of(res.get("last_activity"))
        if task_date is None or (last is not None and last > task_date):
            return f"You were the last to act on it ({last or 'date unknown'})."
    return None


def rows(rc: "RunContext", tg: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolved task candidates that look done, with the reason."""
    cache = read_cache(rc)
    out = []
    df = rc.ws.config.decisions.verify.date_field
    for row in tg["tasks"]:
        res = cache.get(row["ref"]) or {}
        why = looks_done(rc, res, _date_of(row["task"]["fields"].get(df)))
        if why:
            out.append({**row, "resolution": {k: res.get(k) for k in ("source", "state", "last_actor", "last_activity",
                                                                       "title")}, "reason": why})
    return out


def item_notes(rc: "RunContext", tg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cache = read_cache(rc)
    out = {}
    for row in tg["items"]:
        res = cache.get(row["ref"]) or {}
        if res.get("status") == "resolved":
            out[row["item_id"]] = {"ref": row["ref"], "state": res.get("state"), "last_actor": res.get("last_actor"),
                                   "reason": looks_done(rc, res, None)}
    return out


def subject_digest(row: dict[str, Any]) -> str:
    r = row["resolution"]
    return digest({"line": row["task"]["line"], "ref": row["ref"], "state": r.get("state"),
                   "last_actor": r.get("last_actor"), "last_activity": r.get("last_activity")})


def question(rc: "RunContext", row: dict[str, Any]) -> dict[str, Any]:
    cfg = rc.ws.config.decisions
    r = row["resolution"]
    ctx = {"task": row["task"], "ref": row["ref"], "state": r.get("state") or "", "last_actor": r.get("last_actor") or "",
           "last_activity": r.get("last_activity") or "", "ref_title": r.get("title") or "", "reason": row["reason"]}
    labels = {**LABELS, **cfg.verify.labels}
    did = row["task"]["destination"]
    opts = [{"id": oid, "label": templates.render(labels[oid], ctx), "action": ACTIONS[oid],
             "destination": did if oid != "keep" else None, "defaults": {}} for oid in ("close", "keep", "update")]
    return {"id": "v_" + row["task"]["id"].removeprefix("t_"), "kind": "verify", "task_id": row["task"]["id"],
            "prompt": templates.render(cfg.templates.get("verify_prompt", PROMPT), ctx),
            "evidence": [f"Task line: {row['task']['line'].strip()}",
                         f"{row['ref']}: {r.get('state') or 'state unknown'}"
                         + (f", last actor {r['last_actor']}" if r.get("last_actor") else "")
                         + (f" ({r['title']})" if r.get("title") else "")],
            "options": opts, "refused": [], "notes": [], "requires": {"update": ["title|action|due|est"]},
            "subject": subject_digest(row)}


def operations(rc: "RunContext", qid: str, row: dict[str, Any], answer: dict[str, Any]) -> tuple[
        list[dict[str, Any]], dict[str, Any]]:
    from docket.decide.operations import preview, task_update_fields
    oid = answer["option"]
    ov = answer.get("overrides") or {}
    task = row["task"]
    base = {"anchor": task["section"]}
    if oid == "close":
        return preview(rc, "task.close", task["destination"], base, qid, target=task["line"]), {"closed": True}
    if oid == "update":
        fields: dict[str, Any] = {**base, "set": task_update_fields(rc, ov)}
        title = ov.get("action") or ov.get("title")
        if title:
            fields["title"] = title
        adj = {"est": ov["est"]} if ov.get("est") else {}
        return preview(rc, "task.update", task["destination"], fields, qid, target=task["line"]), adj
    return [], {}


__all__ = ["targets", "pending", "resolve_pending", "rows", "question", "operations", "item_notes", "looks_done"]
