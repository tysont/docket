"""Reference and template checks for the ``journal`` and ``hooks`` sections (TD §3.4 passes 2 and 3).

Structure is checked by ``docket.config.close_models``. Here: journal titles,
section headings and templates, ledger keys and fields, and the approval delay
note use only the declared journal context; ``journal.destination`` and a
workflow's ``journal`` name a ``journal`` destination; a workflow's ``hooks``
map names known events and hooks; hook argv elements use only run facts.
"""
from __future__ import annotations

from typing import Any

from docket.config import contexts
from docket.config.close_models import HOOK_EVENTS
from docket.config.loader import Issue, Loaded, closest, dotted, origin_for
from docket.text import templates


def _issue(loaded: Loaded, code: str, msg: str, path: tuple[Any, ...], pass_no: int, **kw: Any) -> Issue:
    org = origin_for(loaded.origins, path)
    return Issue(code, msg, path=dotted(path), file=org.file if org else None,
                 line=org.line if org else None, column=org.column if org else None, pass_no=pass_no, **kw)


def journal_site(ledgers: Any) -> templates.FieldSet:
    """The journal context with each configured ledger as a list of open rows (``ledgers.<name>[]``)."""
    names = sorted(ledgers) if isinstance(ledgers, dict) else []
    extra = [p for n in names for p in (f"ledgers.{n}[]", f"ledgers.{n}[].*")]
    return templates.FieldSet(set(contexts.JOURNAL.paths) | set(extra))


def check(loaded: Loaded) -> list[Issue]:
    d = loaded.data if isinstance(loaded.data, dict) else {}
    out: list[Issue] = []

    def tpl(path: tuple[Any, ...], text: Any, site: templates.FieldSet, wrap: str | None = None) -> None:
        if not isinstance(text, str):
            return
        src = f"{{#{wrap}}}{text}{{/{wrap}}}" if wrap else text
        for err in templates.validate(src, site):
            sugg = closest(err.field_name, site.names()) if err.field_name else None
            out.append(_issue(loaded, "config.bad_template", f"{dotted(path)}: {err.message}", path, 3,
                              suggestion=sugg))

    dests = d.get("destinations") if isinstance(d.get("destinations"), dict) else {}
    journal_dests = sorted(k for k, v in dests.items() if isinstance(v, dict) and v.get("adapter") == "journal")

    def dest_ref(path: tuple[Any, ...], name: Any) -> None:
        if isinstance(name, str) and name not in journal_dests:
            out.append(_issue(loaded, "config.bad_reference",
                              f"{dotted(path)} names {name!r}, which is not a destination with adapter journal",
                              path, 2, suggestion=closest(name, journal_dests)))

    j = d.get("journal") if isinstance(d.get("journal"), dict) else {}
    J = journal_site(j.get("ledgers"))
    tpl(("journal", "title"), j.get("title"), J)
    tpl(("journal", "approval_delay_note"), j.get("approval_delay_note"), J)
    dest_ref(("journal", "destination"), j.get("destination"))
    for i, sec in enumerate(j.get("sections") or [] if isinstance(j.get("sections"), list) else []):
        if isinstance(sec, dict):
            tpl(("journal", "sections", i, "heading"), sec.get("heading"), J)
            tpl(("journal", "sections", i, "template"), sec.get("template"), J)
    led = j.get("ledgers") if isinstance(j.get("ledgers"), dict) else {}
    for name, spec in led.items():
        if not isinstance(spec, dict):
            continue
        frm = spec.get("from") if isinstance(spec.get("from"), str) else None
        if frm is None:
            continue
        tpl(("journal", "ledgers", name, "key"), spec.get("key"), J, frm)
        for f, t in (spec.get("fields") or {}).items() if isinstance(spec.get("fields"), dict) else []:
            tpl(("journal", "ledgers", name, "fields", f), t, J, frm)

    hooks = d.get("hooks") if isinstance(d.get("hooks"), dict) else {}
    wfs = d.get("workflows") if isinstance(d.get("workflows"), dict) else {}
    for hid, h in hooks.items():
        if not isinstance(h, dict):
            continue
        for key in ("command", "dry_run"):
            for i, a in enumerate(h.get(key) or [] if isinstance(h.get(key), list) else []):
                tpl(("hooks", hid, key, i), a, contexts.HOOK_ARG)
        for i, w in enumerate(h.get("workflows") or [] if isinstance(h.get("workflows"), list) else []):
            if isinstance(w, str) and w not in wfs:
                out.append(_issue(loaded, "config.bad_reference", f"hooks.{hid}.workflows names workflow {w!r}, "
                                  "which is not defined", ("hooks", hid, "workflows", i), 2,
                                  suggestion=closest(w, list(wfs))))
    for wname, wf in wfs.items():
        if not isinstance(wf, dict):
            continue
        dest_ref(("workflows", wname, "journal"), wf.get("journal"))
        wh = wf.get("hooks") if isinstance(wf.get("hooks"), dict) else {}
        for ev, ids in wh.items():
            if ev not in HOOK_EVENTS:
                out.append(_issue(loaded, "config.unknown_kind", f"workflows.{wname}.hooks: unknown event {ev!r}",
                                  ("workflows", wname, "hooks", ev), 3, suggestion=closest(str(ev), list(HOOK_EVENTS))))
                continue
            for i, hid in enumerate(ids or [] if isinstance(ids, list) else []):
                if isinstance(hid, str) and hid not in hooks:
                    out.append(_issue(loaded, "config.bad_reference",
                                      f"workflows.{wname}.hooks.{ev} names hook {hid!r}, which is not defined",
                                      ("workflows", wname, "hooks", ev, i), 2, suggestion=closest(hid, list(hooks))))
    return out
