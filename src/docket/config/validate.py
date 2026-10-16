"""The four validation passes (TD §3.4). Every pass reports every error it finds.

1. Syntax and structure: Pydantic models with ``extra="forbid"``.
2. References: every id a value names exists.
3. Semantics: regexes, templates, timezones, mappings, stage order.
4. Capabilities: the adapters configured support what the workflows need.

Passes 2 to 4 read the merged data defensively, so a structural error does not
hide a reference or template error elsewhere.
"""
from __future__ import annotations

import re
import types
import typing
from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

from docket.config import contexts
from docket.config.loader import Issue, Loaded, Path_, closest, dotted, origin_for
from docket.config.models import (
    ACTION_NEEDS_DESTINATION, ACTIONS, DESTINATION_ADAPTERS, STAGES, WHEN_GUARDS, DocketConfig,
)
from docket.text import templates


def _issue(loaded: Loaded, code: str, msg: str, path: Path_, pass_no: int, **kw: Any) -> Issue:
    org = origin_for(loaded.origins, path)
    return Issue(code, msg, path=dotted(path), file=org.file if org else None,
                 line=org.line if org else None, column=org.column if org else None, pass_no=pass_no, **kw)


# --- pass 1 ----------------------------------------------------------------


def _unwrap(tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:
        return _unwrap(typing.get_args(tp)[0])
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return _unwrap(args[0])
    return tp


def _model_at(path: Path_) -> type[BaseModel] | None:
    tp: Any = DocketConfig
    for el in path:
        tp = _unwrap(tp)
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            f = tp.model_fields.get(str(el))
            if f is None:
                return None
            tp = f.annotation
            continue
        origin = typing.get_origin(tp)
        if origin is dict:
            tp = typing.get_args(tp)[1]
        elif origin is list:
            tp = typing.get_args(tp)[0]
        else:
            return None
    tp = _unwrap(tp)
    return tp if isinstance(tp, type) and issubclass(tp, BaseModel) else None


def _data_path(data: Any, loc: tuple[Any, ...]) -> Path_:
    out: list[Any] = []
    cur = data
    for el in loc:
        if isinstance(cur, dict) and el in cur:
            cur = cur[el]
            out.append(el)
        elif isinstance(cur, list) and isinstance(el, int) and 0 <= el < len(cur):
            cur = cur[el]
            out.append(el)
        elif isinstance(el, str) and isinstance(cur, dict):
            out.append(el)  # a missing key: the path ends here
            break
        else:
            continue  # union tags and the like name no data
    return tuple(out)


def pass_structure(loaded: Loaded) -> tuple[DocketConfig | None, list[Issue]]:
    try:
        return DocketConfig.model_validate(loaded.data), []
    except ValidationError as e:
        issues: list[Issue] = []
        for err in e.errors():
            loc = tuple(err["loc"])
            path = _data_path(loaded.data, loc)
            t = err["type"]
            if t == "extra_forbidden":
                key = path[-1] if path else loc[-1]
                model = _model_at(path[:-1])
                fields = list(model.model_fields) if model else []
                issues.append(_issue(loaded, "config.unknown_key", f"unknown key {key!r} in {dotted(path[:-1]) or 'the top level'}",
                                     path, 1, suggestion=closest(str(key), fields)))
            elif t == "missing":
                issues.append(_issue(loaded, "config.missing_key", f"required key {dotted(path)!r} is missing",
                                     path[:-1] if path else path, 1))
            else:
                expected = err.get("ctx", {}).get("expected") if isinstance(err.get("ctx"), dict) else None
                issues.append(_issue(loaded, "config.bad_type", f"{dotted(path) or 'value'}: {err['msg']}", path, 1,
                                     expected=str(expected) if expected else None))
        return None, issues


# --- defensive accessors ----------------------------------------------------


def _get(d: Any, *keys: Any, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and isinstance(k, int) and 0 <= k < len(cur):
            cur = cur[k]
        else:
            return default
    return cur


def _dicts(v: Any) -> Iterable[tuple[int, dict]]:
    if isinstance(v, list):
        for i, x in enumerate(v):
            if isinstance(x, dict):
                yield i, x


def _items(v: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(v, dict):
        yield from v.items()


def _ids(v: Any) -> list[str]:
    return [str(x["id"]) for _, x in _dicts(v) if "id" in x]


# --- pass 2 ----------------------------------------------------------------


def pass_references(loaded: Loaded) -> list[Issue]:
    d = loaded.data
    out: list[Issue] = []
    prof = _get(d, "profile", default={})

    def ref(value: Any, known: list[str], path: Path_, what: str, extra: tuple[str, ...] = ()) -> None:
        if value is None or not isinstance(value, str):
            return
        if value in known or value in extra:
            return
        out.append(_issue(loaded, "config.bad_reference", f"{dotted(path)} names {what} {value!r}, which is not defined",
                          path, 2, suggestion=closest(value, list(known) + list(extra))))

    for sect in ("people", "groups", "topics", "entities", "forums"):
        seen: dict[str, int] = {}
        for i, x in _dicts(_get(prof, sect)):
            pid = x.get("id")
            if isinstance(pid, str):
                if pid in seen:
                    out.append(_issue(loaded, "config.duplicate_id", f"profile.{sect} has two entries with id {pid!r}",
                                      ("profile", sect, i, "id"), 2))
                seen[pid] = i

    people = _ids(_get(prof, "people"))
    groups_l = _get(prof, "groups")
    forums = _ids(_get(prof, "forums"))
    sources = [k for k, _ in _items(_get(d, "sources"))]
    dests = [k for k, _ in _items(_get(d, "destinations"))]
    option_sets = [k for k, _ in _items(_get(d, "decisions", "option_sets"))]

    ref(_get(prof, "me", "reports_to"), people, ("profile", "me", "reports_to"), "person")
    for i, p in _dicts(_get(prof, "people")):
        ref(p.get("reports_to"), people, ("profile", "people", i, "reports_to"), "person", ("me",))
    for i, g in _dicts(groups_l):
        for j, mem in enumerate(g.get("members") or []):
            ref(mem, people, ("profile", "groups", i, "members", j), "person", ("me",))
        ref(g.get("forum"), forums, ("profile", "groups", i, "forum"), "forum")
    for i, f in _dicts(_get(prof, "forums")):
        ref(_get(f, "agenda", "destination"), dests, ("profile", "forums", i, "agenda", "destination"), "destination")

    for wname, wf in _items(_get(d, "workflows")):
        if not isinstance(wf, dict):
            continue
        wsrc = wf.get("sources")
        for j, s in enumerate(wsrc or [] if isinstance(wsrc, list) else []):
            ref(s, sources, ("workflows", wname, "sources", j), "source")
        allowed = wsrc if isinstance(wsrc, list) else sources
        for j, s in enumerate(wf.get("refresh_on_resume") or []):
            ref(s, [x for x in allowed if isinstance(x, str)], ("workflows", wname, "refresh_on_resume", j),
                "source of this workflow")
        ref(wf.get("options"), option_sets, ("workflows", wname, "options"), "option set")

    for sname, kinds in _items(_get(d, "decisions", "option_sets")):
        for kind, opts in _items(kinds):
            seen_opt: set[str] = set()
            for i, o in _dicts(opts):
                oid = o.get("id")
                if isinstance(oid, str):
                    if oid in seen_opt:
                        out.append(_issue(loaded, "config.duplicate_id", f"option set {sname}.{kind} has two options {oid!r}",
                                          ("decisions", "option_sets", sname, kind, i, "id"), 2))
                    seen_opt.add(oid)
                ref(o.get("destination"), dests, ("decisions", "option_sets", sname, kind, i, "destination"), "destination")
    return out


# --- pass 3 ----------------------------------------------------------------


def _check_template(loaded: Loaded, tpl: Any, site: templates.FieldSet, path: Path_, out: list[Issue]) -> None:
    if not isinstance(tpl, str):
        return
    for err in templates.validate(tpl, site):
        sugg = closest(err.field_name, site.names()) if err.field_name else None
        where = f" at character {err.pos + 1}" if err.pos is not None else ""
        out.append(_issue(loaded, "config.bad_template", f"{dotted(path)}: {err.message}{where}", path, 3,
                          suggestion=sugg))


def _check_regex(loaded: Loaded, rx: Any, path: Path_, out: list[Issue]) -> None:
    if not isinstance(rx, str):
        return
    try:
        re.compile(rx)
    except re.error as e:
        out.append(_issue(loaded, "config.bad_regex", f"{dotted(path)}: {e}", path, 3))


def _check_destination(loaded: Loaded, dname: str, dest: dict, out: list[Issue]) -> None:
    """Destination templates, marker, grammar, redaction patterns and adapter requirements (TD §11)."""
    from docket.destinations import sites
    base = ("destinations", dname)
    adapter = dest.get("adapter") if isinstance(dest.get("adapter"), str) else ""
    line_site = sites.line_site(adapter) or contexts.TASK_LINE
    _check_template(loaded, dest.get("line_template"), line_site, base + ("line_template",), out)
    text_site = sites.text_site(adapter)
    for key in ("title_template", "notes_template"):
        if text_site is not None:
            _check_template(loaded, dest.get(key), text_site, base + (key,), out)
    if adapter == "journal":
        _check_template(loaded, dest.get("section"), sites.JOURNAL_HEADING, base + ("section",), out)
    marker = dest.get("marker")
    if isinstance(marker, str):
        _check_template(loaded, marker, sites.MARKER, base + ("marker",), out)
        if "{key}" not in marker:
            out.append(_issue(loaded, "config.bad_template", f"{dotted(base + ('marker',))}: a marker must contain {{key}}",
                              base + ("marker",), 3))
    gram = dest.get("grammar")
    if isinstance(gram, dict):
        for k in ("open", "separator", "assign"):
            if k in gram and isinstance(gram[k], str) and not gram[k]:
                out.append(_issue(loaded, "config.bad_type", f"{dotted(base + ('grammar', k))} must not be empty",
                                  base + ("grammar", k), 3))
        if gram.get("fields") == "delimited" and gram.get("separator") is None:
            out.append(_issue(loaded, "config.missing_key", f"{dotted(base + ('grammar',))}: delimited fields need a separator",
                              base + ("grammar",), 3))
    red = dest.get("redact")
    if isinstance(red, dict):
        for i, rx in enumerate(red.get("figures") or []):
            _check_regex(loaded, rx, base + ("redact", "figures", i), out)
        for cname, tpl in _items(red.get("classes")):
            _check_template(loaded, tpl, sites.CLASS_TITLE, base + ("redact", "classes", cname), out)
    cap = dest.get("capacity")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap < 1:
        out.append(_issue(loaded, "config.bad_type", f"{dotted(base + ('capacity',))} must be at least 1",
                          base + ("capacity",), 3))
    if adapter == "calendar_exec" and not dest.get("program"):
        out.append(_issue(loaded, "config.missing_key", f"destination {dname!r} of adapter calendar_exec needs a program",
                          base, 3))
    need = {"calendar_mcp": ("list",), "message_mcp": ("list", "draft")}.get(adapter, ())
    for tool in need:
        if tool not in (dest.get("tools") or {}):
            out.append(_issue(loaded, "config.missing_key", f"destination {dname!r} needs tools.{tool}", base, 3))


def pass_semantics(loaded: Loaded) -> list[Issue]:
    d = loaded.data
    out: list[Issue] = []
    tz = _get(d, "profile", "timezone")
    if isinstance(tz, str):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            out.append(_issue(loaded, "config.bad_timezone", f"unknown timezone {tz!r}", ("profile", "timezone"), 3))

    for i, rule in _dicts(_get(d, "profile", "calendar", "classify")):
        _check_regex(loaded, _get(rule, "match", "title_regex"), ("profile", "calendar", "classify", i, "match", "title_regex"), out)
    for i, f in _dicts(_get(d, "profile", "forums")):
        _check_regex(loaded, _get(f, "calendar_match", "title_regex"), ("profile", "forums", i, "calendar_match", "title_regex"), out)

    for dname, dest in _items(_get(d, "destinations")):
        if isinstance(dest, dict):
            _check_destination(loaded, dname, dest, out)
    for tname, tpl in _items(_get(d, "decisions", "templates")):
        site = contexts.DECISION_TEMPLATE_SITES.get(tname)
        if site is None:
            out.append(_issue(loaded, "config.unknown_key", f"no template site named {tname!r}",
                              ("decisions", "templates", tname), 3,
                              suggestion=closest(tname, list(contexts.DECISION_TEMPLATE_SITES))))
            continue
        _check_template(loaded, tpl, site, ("decisions", "templates", tname), out)
    for sname, kinds in _items(_get(d, "decisions", "option_sets")):
        for kind, opts in _items(kinds):
            for i, o in _dicts(opts):
                base = ("decisions", "option_sets", sname, kind, i)
                _check_template(loaded, o.get("label"), contexts.OPTION_LABEL, base + ("label",), out)
                act = o.get("action")
                if isinstance(act, str) and act not in ACTIONS:
                    out.append(_issue(loaded, "config.unknown_kind", f"unknown action {act!r}", base + ("action",), 3,
                                      suggestion=closest(act, list(ACTIONS))))
                when = o.get("when")
                if isinstance(when, str) and when.split(":", 1)[0] not in WHEN_GUARDS:
                    out.append(_issue(loaded, "config.unknown_kind", f"unknown guard {when!r}", base + ("when",), 3,
                                      suggestion=closest(when, list(WHEN_GUARDS))))

    for wname, wf in _items(_get(d, "workflows")):
        if not isinstance(wf, dict):
            continue
        stages = wf.get("stages")
        base = ("workflows", wname)
        if isinstance(stages, list) and all(isinstance(s, str) for s in stages) and all(s in STAGES for s in stages):
            idx = [STAGES.index(s) for s in stages]
            problems: list[str] = []
            if len(set(stages)) != len(stages):
                problems.append("a stage is listed twice")
            if idx != sorted(idx):
                problems.append("stages must follow " + " -> ".join(STAGES))
            if not stages or stages[0] != "collect":
                problems.append("the first stage must be collect")
            if not stages or stages[-1] != "close":
                problems.append("the last stage must be close")
            needs = {"approve": "plan", "apply": "approve", "plan": "decide", "decide": "assess", "review": "assess",
                     "merge": "assess"}
            for s, req in needs.items():
                if s in stages and req not in stages:
                    problems.append(f"{s} needs {req}")
            for p in problems:
                out.append(_issue(loaded, "config.bad_stage_order", f"workflow {wname!r}: {p}", base + ("stages",), 3))
            pb = wf.get("pause_before")
            if isinstance(pb, str) and pb in STAGES and (pb not in stages or pb == "collect"):
                out.append(_issue(loaded, "config.bad_stage_order",
                                  f"workflow {wname!r}: pause_before {pb!r} must name a stage of the workflow after collect",
                                  base + ("pause_before",), 3))
    return out


# --- pass 4 ----------------------------------------------------------------


def pass_capabilities(loaded: Loaded) -> list[Issue]:
    d = loaded.data
    out: list[Issue] = []
    dests = dict(_items(_get(d, "destinations")))
    for dname, dest in dests.items():
        ad = _get(dest, "adapter")
        if isinstance(ad, str) and ad not in DESTINATION_ADAPTERS:
            out.append(_issue(loaded, "config.unknown_kind", f"unknown destination adapter {ad!r}",
                              ("destinations", dname, "adapter"), 4, suggestion=closest(ad, list(DESTINATION_ADAPTERS))))
        pol = _get(dest, "policy", "send")
        if pol is True:
            out.append(_issue(loaded, "config.capability_missing", "sending is not in v1; destinations create drafts only",
                              ("destinations", dname, "policy", "send"), 4))

    def accepts(action: str) -> list[str]:
        def ok(x: Any) -> bool:
            ad = _get(x, "adapter") or ""
            if ad == "calendar_mcp" and "create" not in (_get(x, "tools") or {}):
                return False  # an MCP calendar writes only where the preset declares the tool
            return action in DESTINATION_ADAPTERS.get(ad, frozenset())
        return [n for n, x in dests.items() if ok(x)]

    for wname, wf in _items(_get(d, "workflows")):
        stages = _get(wf, "stages") or []
        if not isinstance(stages, list) or "apply" not in stages:
            continue
        set_name = _get(wf, "options") or "default"
        for kind, opts in _items(_get(d, "decisions", "option_sets", set_name)):
            for i, o in _dicts(opts):
                act = o.get("action")
                if act not in ACTION_NEEDS_DESTINATION:
                    continue
                target = o.get("destination")
                ok = accepts(act)
                if (target and target in dests and target not in ok) or (not target and not ok):
                    out.append(_issue(
                        loaded, "config.capability_missing",
                        f"workflow {wname!r} offers action {act!r}, but no destination"
                        + (f" {target!r}" if target else "") + " accepts it",
                        ("decisions", "option_sets", set_name, kind, i, "action"), 4))
    return out


def validate(loaded: Loaded) -> tuple[DocketConfig | None, list[Issue]]:
    issues = list(loaded.issues)
    model, p1 = pass_structure(loaded)
    issues += p1
    issues += pass_references(loaded)
    issues += pass_semantics(loaded)
    issues += pass_capabilities(loaded)
    from docket.config.source_checks import check as check_sources  # sources, transports, signals.units
    issues += check_sources(loaded)
    return (model if not issues else None), issues
