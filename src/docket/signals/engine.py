"""The rule engine (TD §8.1, FR-SIG-01..03, FR-SIG-06).

``evaluate(items, env)`` returns enriched copies of mapped items:

- ``sender``: the resolved sender (``docket.items.identity``);
- ``mentioned``: operator, people, groups, topics and entities named in the text;
- ``positions``: the operator's position (``direct``, ``cc``, ``mentioned``, ``member``);
- ``references``: identifiers matched by ``signals.references`` patterns;
- ``keys``: the identity keys merge groups on (references, then the thread);
- ``signals``: the combined effect of every matching rule;
- ``trace``: a ``mapped`` entry and one ``rule`` entry per matching rule.

Rules evaluate in file order and every matching rule applies. Contributions
(``importance``, ``urgency``) add up; ``tags``, ``reason`` and ``expect_in``
accumulate; ``route``, ``area``, ``topic``, ``sensitivity`` and ``deadline`` take the
last matching rule's value. ``surface: always`` beats ``dismiss``: the item is not
dismissed and the trace records the overridden dismissal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from docket.cli.errors import DocketError
from docket.config.models import Profile
from docket.items.identity import ProfileIndex
from docket.shapes.unexpected import unexpected
from docket.signals import plugins
from docket.signals.models import FieldPred, Rule, SignalsConfig, Then, When


@lru_cache(maxsize=2048)
def rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def instant(v: Any) -> datetime | None:
    """A tolerant ISO parse; a naive value reads as UTC. None when unparseable."""
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def at_key(it: dict[str, Any]) -> tuple[int, float]:
    """Sort key on ``at``: dated items in time order, undated last."""
    t = instant(it.get("at"))
    return (0, t.timestamp()) if t else (1, 0.0)


def minutes(v: str | int) -> int:
    from docket.text.durations import parse_duration
    return parse_duration(v)


@dataclass
class SignalEnv:
    cfg: SignalsConfig
    profile: Profile
    origins: list[str] = field(default_factory=list)
    units: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    source_kinds: dict[str, str] = field(default_factory=dict)
    now: datetime | None = None
    run_date: date | None = None
    index: ProfileIndex = field(init=False)

    def __post_init__(self) -> None:
        self.index = ProfileIndex(self.profile)


def env_for(ws: Any, *, units: dict[tuple[str, str], dict[str, Any]] | None = None, now: str | None = None,
            run_date: str | None = None) -> SignalEnv:
    from docket.config.source_checks import source_kind
    from docket.signals.models import rule_origins, signals_config
    cfg = signals_config(ws)
    raw_sources = ws.resolved.get("sources") or {}
    raw_transports = ws.resolved.get("transports") or {}
    kinds = {sid: (source_kind(s, raw_transports) or "files") for sid, s in raw_sources.items() if isinstance(s, dict)}
    return SignalEnv(cfg, ws.config.profile, rule_origins(ws, cfg), units or {}, kinds, instant(now),
                     date.fromisoformat(run_date) if run_date else None)


# --- enrichment ----------------------------------------------------------------


def _field_value(it: dict[str, Any], name: str) -> Any:
    if name.startswith("fields."):
        return (it.get("fields") or {}).get(name[7:])
    if name in ("title", "text", "url", "identity", "unit", "from", "handle"):
        return it.get(name)
    return (it.get("fields") or {}).get(name)


def references(it: dict[str, Any], cfg: SignalsConfig) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, pat in cfg.references.items():
        r = rx(pat.pattern)
        for f in pat.fields:
            v = _field_value(it, f)
            if v in (None, ""):
                continue
            for m in r.finditer(str(v)):
                val = m.group("id") if "id" in r.groupindex else m.group(0)
                if val and (name, val) not in seen:
                    seen.add((name, val))
                    out.append({"pattern": name, "value": val})
    return out


def _threads(items: list[dict[str, Any]], env: SignalEnv) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (source, thread): who started it, whether and when the operator replied, how many records."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for it in items:
        if it.get("thread"):
            groups.setdefault((it["source"], it["thread"]), []).append(it)
    out = {}
    for key, rows in groups.items():
        rows = sorted(rows, key=lambda r: (*at_key(r), r["id"]))
        mine = [instant(r.get("at")) for r in rows if r["sender"]["is_me"]]
        out[key] = {"started_by_me": rows[0]["sender"]["is_me"], "i_replied": bool(mine), "count": len(rows),
                    "my_times": [t for t in mine if t is not None]}
    return out


def _positions(it: dict[str, Any], env: SignalEnv) -> list[str]:
    me = env.index.me_handles
    pos = []
    to = {h.strip().lower() for h in it.get("to") or []}
    cc = {h.strip().lower() for h in it.get("cc") or []}
    ukind = (env.units.get((it["source"], it.get("unit") or "")) or {}).get("kind")
    if to & me or ukind == "direct":
        pos.append("direct")
    if cc & me:
        pos.append("cc")
    if it["mentioned"]["me"]:
        pos.append("mentioned")
    if not pos or pos == ["mentioned"]:
        pos.append("member")
    return pos


def enrich(items: list[dict[str, Any]], env: SignalEnv) -> list[dict[str, Any]]:
    out = []
    for raw in items:
        it = dict(raw)
        it["sender"] = env.index.sender(it.get("handle") or it.get("from"), it.get("from"))
        hay = f"{it.get('title') or ''}\n{it.get('text') or ''}"
        it["mentioned"] = env.index.mentions(hay, it.get("mentions"))
        it["positions"] = _positions(it, env)
        it["references"] = references(it, env.cfg)
        keys = [f"ref:{r['pattern']}:{r['value']}" for r in it["references"]]
        if it.get("thread"):
            keys.append(f"thread:{it['source']}:{it['thread']}")
        it["keys"] = keys
        it["trace"] = [{"stage": "collect", "step": "mapped", "source": it["source"], "unit": it.get("unit") or "",
                        "record_id": it.get("record_id"), "identity": it.get("identity")}]
        out.append(it)
    return out


# --- predicates ------------------------------------------------------------------


def _in(v: Any, allowed: list[Any] | None) -> bool:
    return allowed is None or v in allowed


def _age_minutes(it: dict[str, Any], env: SignalEnv) -> float | None:
    at = instant(it.get("at"))
    if at is None or env.now is None:
        return None
    return (env.now - at).total_seconds() / 60.0


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _eq(a: Any, b: Any) -> bool:
    if a == b:
        return True
    return a is not None and b is not None and str(a).strip().lower() == str(b).strip().lower()


def _field_ok(fp: FieldPred, it: dict[str, Any]) -> bool:
    v = _field_value(it, fp.name)
    present = v not in (None, "", [])
    if fp.present is not None and present != fp.present:
        return False
    if "equals" in fp.model_fields_set and not _eq(v, fp.equals):
        return False
    if fp.in_ is not None and not any(_eq(v, x) for x in fp.in_):
        return False
    if fp.regex is not None and (v is None or not rx(fp.regex).search(str(v))):
        return False
    if fp.min is not None or fp.max is not None:
        n = _num(v)
        if n is None or (fp.min is not None and n < fp.min) or (fp.max is not None and n > fp.max):
            return False
    return True


def _sel(found: list[str], ids: list[str] | None, ok: Any) -> bool:
    return any((ids is None or x in ids) and ok(x) for x in found)


def match(w: When, it: dict[str, Any], env: SignalEnv, threads: dict[tuple[str, str], dict[str, Any]]) -> bool:
    if w.all_ is not None and not all(match(x, it, env, threads) for x in w.all_):
        return False
    if w.any_ is not None and not any(match(x, it, env, threads) for x in w.any_):
        return False
    if w.not_ is not None and match(w.not_, it, env, threads):
        return False
    if w.source is not None:
        if w.source.id is not None and it["source"] not in w.source.id:
            return False
        if w.source.kind is not None and env.source_kinds.get(it["source"]) not in w.source.kind:
            return False
    if w.unit is not None:
        up = w.unit
        u = env.units.get((it["source"], it.get("unit") or "")) or {}
        uid = it.get("unit") or ""
        if up.id is not None and uid not in up.id:
            return False
        if up.id_regex is not None and not rx(up.id_regex).search(uid):
            return False
        if up.name_regex is not None and not rx(up.name_regex).search(str(u.get("name") or "")):
            return False
        if up.kind is not None and u.get("kind") not in up.kind:
            return False
        if up.class_ is not None and u.get("class") not in up.class_:
            return False
        tier = u.get("tier")
        if up.tier is not None and str(tier) not in {str(t) for t in up.tier}:
            return False
        if up.tier_max is not None and (_num(tier) is None or _num(tier) > up.tier_max):  # type: ignore[operator]
            return False
    if w.from_ is not None:
        f, s = w.from_, it["sender"]
        rels = set(s["relations"]) | {s["relation"]}
        if f.relation is not None and not rels & set(f.relation):
            return False
        if f.person is not None and s["person"] not in f.person:
            return False
        if f.group is not None and not rels & {f"group:{g}" for g in f.group}:
            return False
        if f.domain is not None:
            d = s["domain"] or ""
            if not any(d == x.lower() or d.endswith("." + x.lower()) for x in f.domain):
                return False
        if f.handle is not None and not any(rx(h, re.I).search(s["handle"] or "") for h in f.handle):
            return False
        if f.is_me is not None and s["is_me"] != f.is_me:
            return False
    if w.to is not None and not set(it["positions"]) & set(w.to.position):
        return False
    if w.mentions is not None and not _mentions_ok(w, it, env):
        return False
    if w.text is not None:
        t = w.text
        hay = "\n".join(str(it.get(k) or "") for k in t.fields)
        flags = re.I if t.case == "insensitive" else 0
        hit = False
        if t.regex is not None and any(rx(r, flags).search(hay) for r in t.regex):
            hit = True
        if not hit and t.phrases is not None:
            h = hay.lower() if flags else hay
            hit = any((p.lower() if flags else p) in h for p in t.phrases)
        if not hit:
            return False
    if w.field_ is not None and not all(_field_ok(fp, it) for fp in w.field_):
        return False
    if w.age is not None:
        age = _age_minutes(it, env)
        if age is None:
            return False
        if w.age.older_than is not None and age < minutes(w.age.older_than):
            return False
        if w.age.newer_than is not None and age >= minutes(w.age.newer_than):
            return False
    if w.unanswered is not None and not _unanswered(it, env, threads, minutes(w.unanswered.for_)):
        return False
    if w.thread is not None:
        th = w.thread
        st = threads.get((it["source"], it.get("thread") or ""), {"started_by_me": it["sender"]["is_me"],
                                                                     "i_replied": it["sender"]["is_me"], "count": 1})
        replies = st["count"] - 1
        if th.started_by_me is not None and st["started_by_me"] != th.started_by_me:
            return False
        if th.i_replied is not None and st["i_replied"] != th.i_replied:
            return False
        if th.replies_min is not None and replies < th.replies_min:
            return False
        if th.replies_max is not None and replies > th.replies_max:
            return False
    if w.reference is not None and not any(r["pattern"] in w.reference.pattern for r in it["references"]):
        return False
    if w.plugin is not None and not _plugin(w.plugin.name, w.plugin.args, it):
        return False
    return True


def _mentions_ok(w: When, it: dict[str, Any], env: SignalEnv) -> bool:
    m, mp, ix = it["mentioned"], w.mentions, env.index
    assert mp is not None
    if mp.me is not None and m["me"] != mp.me:
        return False
    if mp.people is not None:
        rel = mp.people.relation
        if not _sel(m["people"], mp.people.id,
                    lambda p: rel is None or bool(set(ix.person_sets.get(p, [])) & set(rel))):
            return False
    if mp.groups is not None and not set(m["groups"]) & set(mp.groups):
        return False
    if mp.topics is not None:
        ts = mp.topics
        if not _sel(m["topics"], ts.id, lambda t: (ts.owned is None or ix.topics[t].owned == ts.owned)
                    and (ts.area is None or ix.topics[t].area in ts.area)):
            return False
    if mp.entities is not None:
        es = mp.entities
        if not _sel(m["entities"], es.id, lambda e: (es.protected is None or ix.entities[e].protected == es.protected)
                    and (es.strategic is None or ix.entities[e].strategic == es.strategic)):
            return False
    return True


def _unanswered(it: dict[str, Any], env: SignalEnv, threads: dict[tuple[str, str], dict[str, Any]], mins: int) -> bool:
    if it["sender"]["is_me"]:
        return False
    at = instant(it.get("at"))
    st = threads.get((it["source"], it.get("thread") or ""))
    if st and at is not None and any(t > at for t in st["my_times"]):
        return False
    if mins <= 0:
        return True
    age = _age_minutes(it, env)
    return age is not None and age >= mins


def _plugin(name: str, args: dict[str, Any], it: dict[str, Any]) -> bool:
    fn = plugins.predicate(name)
    if fn is None:
        unexpected("signals.plugin", f"plugin predicate {name} is not registered",
                   reading="the predicate does not match", extend=f"entry point group {plugins.GROUP}: {name}")
        return False
    try:
        return bool(fn(it, dict(args)))
    except Exception as e:  # a plugin's failure is a shape, not a refusal
        unexpected("signals.plugin", f"plugin predicate {name} raised {type(e).__name__}", sample=str(e)[:200],
                   reading="the predicate does not match", extend=f"entry point group {plugins.GROUP}: {name}")
        return False


# --- effects -----------------------------------------------------------------------


def empty_signals() -> dict[str, Any]:
    return {"importance": 0, "urgency": 0, "surface": False, "surface_rules": [], "dismiss": [], "dismissed": False,
            "route": None, "area": None, "topic": None, "sensitivity": None, "deadline": None, "tags": [],
            "assess": "agent", "assess_rules": [], "reasons": [], "expect_in": [], "rules": []}


def _deadline(t: Then, it: dict[str, Any], env: SignalEnv, rid: str) -> dict[str, Any] | None:
    d = t.deadline
    assert d is not None
    raw: Any = None
    if d.field is not None:
        raw = _field_value(it, d.field if "." in d.field else f"fields.{d.field}")
    else:
        m = rx(d.pattern or "", re.I).search(f"{it.get('title') or ''}\n{it.get('text') or ''}")
        if m:
            raw = m.group("date") if "date" in m.re.groupindex else (m.group(1) if m.re.groups else m.group(0))
    if raw in (None, ""):
        return None
    out: dict[str, Any] = {"raw": str(raw), "date": None, "rule": rid}
    s = str(raw).strip()
    iso = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    try:
        if iso:
            out["date"] = date.fromisoformat(iso.group(1)).isoformat()
        elif env.run_date is not None:
            from docket.text.dates import parse_date
            out["date"] = parse_date(s, env.run_date).isoformat()
    except (DocketError, ValueError):
        pass
    if out["date"] is None:
        unexpected("signals.deadline", "deadline text not a date", sample=s[:80], reading="keep the raw text",
                   extend=f"signals.rules.{rid}.then.deadline")
    return out


def apply(rule: Rule, origin: str, it: dict[str, Any], env: SignalEnv) -> None:
    t, sig = rule.then, it["signals"]
    effects = t.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    entry: dict[str, Any] = {"stage": "signals", "step": "rule", "rule": rule.id, "origin": origin, "effects": effects}
    sig["rules"].append(rule.id)
    if t.importance:
        sig["importance"] += t.importance
    if t.urgency:
        sig["urgency"] += t.urgency
    if t.surface == "always":
        sig["surface"] = True
        sig["surface_rules"].append(rule.id)
    if t.dismiss is not None:
        sig["dismiss"].append({"rule": rule.id, "reason": t.dismiss})
    for k in ("route", "area", "topic", "sensitivity"):
        v = getattr(t, k)
        if v is not None:
            if sig[k] is not None and sig[k] != v:
                entry.setdefault("overrides", {})[k] = sig[k]
            sig[k] = v
    if t.deadline is not None:
        dl = _deadline(t, it, env, rule.id)
        if dl is not None:
            sig["deadline"] = dl
            entry["deadline"] = dl
    for tag in t.tags:
        if tag not in sig["tags"]:
            sig["tags"].append(tag)
    if t.assess == "rules_only":
        sig["assess"] = "rules_only"
        sig["assess_rules"].append(rule.id)
    if t.reason:
        sig["reasons"].append(t.reason)
    if t.expect_in is not None:
        sig["expect_in"].append({"source": t.expect_in.source, "reference": t.expect_in.reference, "rule": rule.id})
    it["trace"].append(entry)


def evaluate(items: list[dict[str, Any]], env: SignalEnv) -> list[dict[str, Any]]:
    out = enrich(items, env)
    threads = _threads(out, env)
    for it in out:
        it["signals"] = empty_signals()
        for i, rule in enumerate(env.cfg.rules):
            if match(rule.when, it, env, threads):
                apply(rule, env.origins[i] if i < len(env.origins) else "core", it, env)
        sig = it["signals"]
        if sig["dismiss"]:
            if sig["surface"]:
                it["trace"].append({"stage": "signals", "step": "surface_over_dismiss",
                                    "surface_rules": sig["surface_rules"],
                                    "dismissed_by": [d["rule"] for d in sig["dismiss"]],
                                    "reason": "; ".join(d["reason"] for d in sig["dismiss"])})
            else:
                sig["dismissed"] = True
    return out


def needs_agent(it: dict[str, Any]) -> bool:
    """An item goes to an assess task unless rules assess it alone or it is dismissed (FR-SIG-06)."""
    sig = it["signals"]
    return sig["assess"] != "rules_only" and not sig["dismissed"]
