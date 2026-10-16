"""Validation of ``signals`` (TD §3.4 passes 1 to 3; ``signals.units`` is checked in
``docket.config.source_checks``). Every error carries file, line and, for an unknown
key, the closest valid key: an unknown predicate is ``config.unknown_predicate``, an
unknown effect ``config.unknown_effect``.
"""
from __future__ import annotations

import re
import typing
from typing import Any

from pydantic import BaseModel, ValidationError

from docket.config.loader import Issue, Loaded, Path_, closest, dotted, origin_for
from docket.signals import models as m

RELATION_WORDS = ("me", "known", "unknown")


def _issue(loaded: Loaded, code: str, msg: str, path: Path_, pass_no: int, **kw: Any) -> Issue:
    org = origin_for(loaded.origins, path)
    return Issue(code, msg, path=dotted(path), file=org.file if org else None,
                 line=org.line if org else None, column=org.column if org else None, pass_no=pass_no, **kw)


def _names(model: type[BaseModel]) -> list[str]:
    return [f.alias or k for k, f in model.model_fields.items()]


def _unwrap(tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType")):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _unwrap(args[0]) if len(args) == 1 else tp
    return tp


def _model_at(path: tuple[Any, ...]) -> type[BaseModel] | None:
    """The model whose field the last element of ``path`` would be."""
    tp: Any = m.SignalsConfig
    for el in path:
        tp = _unwrap(tp)
        if isinstance(tp, str):
            tp = getattr(m, tp.strip("'\""), None)
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            f = next((f for k, f in tp.model_fields.items() if (f.alias or k) == el or k == el), None)
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
    if isinstance(tp, typing.ForwardRef):
        tp = getattr(m, tp.__forward_arg__.split("|")[0].strip(), None)
    return tp if isinstance(tp, type) and issubclass(tp, BaseModel) else None


def _data_loc(data: Any, loc: tuple[Any, ...]) -> tuple[Any, ...]:
    out: list[Any] = []
    cur = data
    for el in loc:
        if isinstance(cur, dict) and el in cur:
            cur = cur[el]
            out.append(el)
        elif isinstance(cur, list) and isinstance(el, int) and 0 <= el < len(cur):
            cur = cur[el]
            out.append(el)
        elif isinstance(cur, dict) and isinstance(el, str) and not el[:1].isupper() and "[" not in el:
            out.append(el)
            break
        elif isinstance(el, int) and not isinstance(cur, list):
            continue  # a shorthand normalized into a list: index 0 is the scalar itself
    return tuple(out)


def _structure(loaded: Loaded, raw: Any) -> list[Issue]:
    try:
        m.SignalsConfig.model_validate(raw)
        return []
    except ValidationError as e:
        out: list[Issue] = []
        seen: set[tuple[str, str]] = set()
        for err in e.errors():
            loc = tuple(err["loc"])
            if loc[:1] == ("units",):
                continue  # checked with the source sections
            dloc = _data_loc(raw, loc)
            path = ("signals",) + dloc
            t = err["type"]
            if t == "extra_forbidden":
                key = str(dloc[-1]) if dloc else str(loc[-1])
                model = _model_at(dloc[:-1])
                fields = _names(model) if model else []
                if model is m.When:
                    code, what, fields = "config.unknown_predicate", "predicate", list(m.PREDICATES + m.COMBINATORS)
                elif model is m.Then:
                    code, what, fields = "config.unknown_effect", "effect", list(m.EFFECTS)
                else:
                    code, what = "config.unknown_key", "key"
                msg = f"unknown {what} {key!r} in {dotted(path[:-1])}"
                if what != "key":
                    msg += f" (one of: {', '.join(fields)})"
                issue = _issue(loaded, code, msg, path, 1, suggestion=closest(key, fields))
            elif t == "missing":
                issue = _issue(loaded, "config.missing_key", f"required key {dotted(path)!r} is missing",
                               path[:-1], 1)
            else:
                issue = _issue(loaded, "config.bad_type", f"{dotted(path)}: {err['msg']}", path, 1)
            k = (issue.code, issue.path + issue.message)
            if k not in seen:
                seen.add(k)
                out.append(issue)
        return out


def _walk(w: m.When, path: Path_) -> typing.Iterator[tuple[m.When, Path_]]:
    yield w, path
    for i, x in enumerate(w.all_ or []):
        yield from _walk(x, path + ("all", i))
    for i, x in enumerate(w.any_ or []):
        yield from _walk(x, path + ("any", i))
    if w.not_ is not None:
        yield from _walk(w.not_, path + ("not",))


def check(loaded: Loaded) -> list[Issue]:
    d = loaded.data
    raw = d.get("signals")
    if not isinstance(raw, dict):
        return []
    out = _structure(loaded, raw)
    if out:
        return out
    cfg = m.parse(raw)
    prof = d.get("profile") if isinstance(d.get("profile"), dict) else {}

    def ids(sect: str) -> list[str]:
        return [str(x["id"]) for x in (prof.get(sect) or []) if isinstance(x, dict) and "id" in x]

    people, groups, topics, entities = ids("people"), ids("groups"), ids("topics"), ids("entities")
    roles = sorted({f"role:{r}" for x in (prof.get("people") or []) if isinstance(x, dict)
                    for r in (x.get("roles") or [])})
    relations = ["chain", "reports", "skip_reports", "peers", "external", *RELATION_WORDS,
                 *[f"group:{g}" for g in groups], *roles]
    sources = list((d.get("sources") or {}).keys()) if isinstance(d.get("sources"), dict) else []
    dests = d.get("destinations") if isinstance(d.get("destinations"), dict) else {}
    options = sorted({str(o.get("id")) for kinds in ((d.get("decisions") or {}).get("option_sets") or {}).values()
                      if isinstance(kinds, dict) for opts in kinds.values() if isinstance(opts, list)
                      for o in opts if isinstance(o, dict) and o.get("id")})
    refs = list(cfg.references)

    def ref(value: str | None, known: list[str], path: Path_, what: str) -> None:
        if value is not None and value not in known:
            out.append(_issue(loaded, "config.bad_reference", f"{dotted(path)} names {what} {value!r}, which is not "
                              "defined", path, 2, suggestion=closest(value, known)))

    def refs_list(values: list[str] | None, known: list[str], path: Path_, what: str) -> None:
        for i, v in enumerate(values or []):
            ref(v, known, path + (i,) if len(values or []) > 1 else path, what)

    def regex(rx: str | None, path: Path_) -> None:
        if rx is None:
            return
        try:
            re.compile(rx)
        except re.error as e:
            out.append(_issue(loaded, "config.bad_regex", f"{dotted(path)}: {e}", path, 3))

    def duration(v: Any, path: Path_) -> None:
        from docket.cli.errors import DocketError
        from docket.text.durations import parse_duration
        if v is None:
            return
        try:
            parse_duration(v)
        except DocketError:
            out.append(_issue(loaded, "config.bad_type", f"{dotted(path)}: not a duration: {v!r}", path, 3,
                              expected="a duration like 45m, 2h or 3d"))

    seen_ids: dict[str, int] = {}
    sensitivity: dict[str, Path_] = {}
    for i, rule in enumerate(cfg.rules):
        base: Path_ = ("signals", "rules", i)
        if rule.id in seen_ids:
            out.append(_issue(loaded, "config.duplicate_id", f"signals.rules has two rules with id {rule.id!r}",
                              base + ("id",), 2))
        seen_ids[rule.id] = i
        for w, wp in _walk(rule.when, base + ("when",)):
            if w.source is not None:
                refs_list(w.source.id, sources, wp + ("source",), "source")
            if w.unit is not None:
                regex(w.unit.id_regex, wp + ("unit", "id_regex"))
                regex(w.unit.name_regex, wp + ("unit", "name_regex"))
            if w.from_ is not None:
                f = w.from_
                refs_list(f.relation, relations, wp + ("from", "relation"), "relationship set")
                refs_list(f.person, people, wp + ("from", "person"), "person")
                refs_list(f.group, groups, wp + ("from", "group"), "group")
                for j, h in enumerate(f.handle or []):
                    regex(h, wp + ("from", "handle"))
            if w.mentions is not None:
                mp = w.mentions
                if mp.people is not None:
                    refs_list(mp.people.id, people, wp + ("mentions", "people"), "person")
                    refs_list(mp.people.relation, relations, wp + ("mentions", "people", "relation"),
                              "relationship set")
                refs_list(mp.groups, groups, wp + ("mentions", "groups"), "group")
                if mp.topics is not None:
                    refs_list(mp.topics.id, topics, wp + ("mentions", "topics"), "topic")
                if mp.entities is not None:
                    refs_list(mp.entities.id, entities, wp + ("mentions", "entities"), "entity")
            if w.text is not None:
                for rx in w.text.regex or []:
                    regex(rx, wp + ("text",))
            for fp in w.field_ or []:
                regex(fp.regex, wp + ("field", "regex"))
            if w.age is not None:
                duration(w.age.older_than, wp + ("age", "older_than"))
                duration(w.age.newer_than, wp + ("age", "newer_than"))
            if w.unanswered is not None:
                duration(w.unanswered.for_, wp + ("unanswered",))
            if w.reference is not None:
                refs_list(w.reference.pattern, refs, wp + ("reference",), "reference pattern")
            if w.plugin is not None:
                from docket.signals import plugins
                if plugins.predicate(w.plugin.name) is None:
                    p = wp + ("plugin",)
                    out.append(_issue(loaded, "config.unknown_plugin", f"{dotted(p)} names plugin predicate "
                                      f"{w.plugin.name!r}, which no installed plugin registers", p, 2,
                                      suggestion=closest(w.plugin.name, plugins.names()["predicates"])))
        t, tp = rule.then, base + ("then",)
        if t.expect_in is not None:
            ref(t.expect_in.source, sources, tp + ("expect_in",), "source")
            ref(t.expect_in.reference, refs, tp + ("expect_in", "reference"), "reference pattern")
        if t.route is not None:
            ref(t.route, sorted(set(dests) | set(options) | {"verify"}), tp + ("route",), "destination or option")
        if t.topic is not None and topics:
            ref(t.topic, topics, tp + ("topic",), "topic")
        if t.deadline is not None:
            regex(t.deadline.pattern, tp + ("deadline", "pattern"))
        if t.sensitivity is not None:
            sensitivity.setdefault(t.sensitivity, tp + ("sensitivity",))
    for name, pat in cfg.references.items():
        regex(pat.pattern, ("signals", "references", name))
    for sid in cfg.scoring.caps.per_source:
        if sid != "default":
            ref(sid, sources, ("signals", "scoring", "caps", "per_source", sid), "source")
    if cfg.scoring.formula == "plugin":
        from docket.signals import plugins
        p = ("signals", "scoring", "plugin")
        if not cfg.scoring.plugin:
            out.append(_issue(loaded, "config.missing_key", "signals.scoring: formula plugin needs plugin", p[:-1], 3))
        elif plugins.formula(cfg.scoring.plugin) is None:
            out.append(_issue(loaded, "config.unknown_plugin", f"signals.scoring.plugin names formula "
                              f"{cfg.scoring.plugin!r}, which no installed plugin registers", p, 2,
                              suggestion=closest(cfg.scoring.plugin, plugins.names()["formulas"])))
    # pass 3: a shared destination with class templates has one for every sensitivity class the rules
    # can assign (TD §3.4). One without class templates redacts entities and people in every title.
    for dname, dest in dests.items():
        if not isinstance(dest, dict) or dest.get("visibility") != "shared":
            continue
        classes = (dest.get("redact") or {}).get("classes") or {}
        if not classes:
            continue
        for cls, where in sorted(sensitivity.items()):
            if cls not in classes:
                out.append(_issue(loaded, "config.missing_key",
                                  f"shared destination {dname!r} has no redaction template for sensitivity class "
                                  f"{cls!r}, which {dotted(where)} assigns", ("destinations", dname, "redact"), 3))
    return out
