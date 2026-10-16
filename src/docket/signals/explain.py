"""``docket item why`` and ``docket item list`` over a merge artifact (TD §8.4, FR-SIG-09).

A reference resolves, in order, to an item id (a topic's or a merged member's),
a topic id, a URL, a record identity, a reference value carried by one item, then
a unique case-insensitive substring of a title or text.
The explanation is the item's trace: mapping, every matching rule with its
file position, the assess outcome, score components, cap and floor decisions,
merges, suppressions, plus the later stages' verdict, question and answer when
those exist.
"""
from __future__ import annotations

from typing import Any

from docket.cli.errors import InputError
from docket.config.loader import closest


def _all(data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    out: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for it in data.get("items") or []:
        out.append((it, None))
        for mem in it.get("members") or []:
            out.append((it, mem))
    return out


def find(data: dict[str, Any], ref: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    rows = _all(data)
    r = ref.strip()
    for key in ("id", "topic", "url", "identity"):
        for top, mem in rows:
            row = mem or top
            if r and str(row.get(key) or "") == r:
                return top, mem
    by_ref = {(t["id"], (m or {}).get("id")): (t, m) for t, m in rows
              if any(x.get("value") == r for x in (m or t).get("references") or [])}
    if len(by_ref) == 1:
        return next(iter(by_ref.values()))
    low = r.lower()
    hits = [(t, m) for t, m in rows if low and low in f"{(m or t).get('title') or ''}\n{(m or t).get('text') or ''}".lower()]
    uniq = {(t["id"], (m or {}).get("id")): (t, m) for t, m in hits}
    if len(uniq) == 1:
        return next(iter(uniq.values()))
    if len(uniq) > 1:
        tops = {t["id"] for t, _ in uniq.values()}
        if len(tops) == 1:  # several members of one topic: explain the topic
            t = next(iter(uniq.values()))[0]
            return t, None
        raise InputError("item.ambiguous", f"{len(uniq)} items match {ref!r}; give an id or more words.",
                         hint="docket item list --filter <words>",
                         details={"matches": [{"id": (m or t)["id"], "title": (m or t).get("title", "")[:100]}
                                              for t, m in list(uniq.values())[:20]]})
    ids = [(m or t)["id"] for t, m in rows]
    raise InputError("item.unknown", f"No item matches {ref!r} in this run.", hint="docket item list",
                     details={"suggestion": closest(r, ids)})


def _fmt(v: Any) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{k} {_fmt(x)}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(_fmt(x) for x in v)
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return str(v)


def line(e: dict[str, Any]) -> str:
    st, step = e.get("stage"), e.get("step")
    if step == "rule":
        eff = e.get("effects") or {}
        parts = [f"{k} {('+' if isinstance(v, int) and not isinstance(v, bool) and v > 0 else '')}{_fmt(v)}"
                 for k, v in eff.items()]
        if e.get("deadline"):
            dl = e["deadline"]
            parts.append(f"extracted deadline {dl.get('date') or dl.get('raw')}")
        if e.get("overrides"):
            parts.append(f"overrides {_fmt(e['overrides'])}")
        return f"[{st}] rule {e.get('rule')} ({e.get('origin')}): " + ("; ".join(parts) or "no effects")
    if step == "surface_over_dismiss":
        return (f"[{st}] surface: always ({_fmt(e.get('surface_rules'))}) overrides dismiss by "
                f"{_fmt(e.get('dismissed_by'))}: {e.get('reason')}")
    if step == "score":
        comp = e.get("components") or {}
        w = e.get("weights") or {}
        terms = ", ".join(f"{k} {comp[k]} (weight {w.get(k)})" for k in comp)
        return f"[{st}] score {e.get('score')} = {e.get('formula')} of {terms}; base {e.get('base')}"
    rest = {k: v for k, v in e.items() if k not in ("stage", "step")}
    return f"[{st}] {step}" + (": " + "; ".join(f"{k} {_fmt(v)}" for k, v in rest.items()) if rest else "")


def later_stages(rc: Any, item_id: str) -> list[dict[str, Any]]:
    """Review verdict, question, answer and operations for the item, when those stages exist."""
    out: list[dict[str, Any]] = []
    for name in ("review", "decide", "approve"):
        if name not in rc.workflow.stages:
            continue
        ev = rc.eval_or_none(name)
        if ev is None or not ev.output:
            continue
        if name == "review":
            row = next((it for it in ev.output.get("items") or [] if it.get("id") == item_id), None)
            if row and row.get("verdict"):
                out.append({"stage": "review", "step": "verdict", "verdict": row["verdict"]})
        elif name == "decide":
            qid = "q_" + item_id
            if any(q["id"] == qid for q in ev.output.get("questions") or []):
                out.append({"stage": "decide", "step": "question", "question": qid})
                ans = (ev.output.get("answers") or {}).get(qid)
                if ans:
                    out.append({"stage": "decide", "step": "answer", "option": ans.get("option"),
                                "action": ans.get("action")})
            elif any(n.get("item_id") == item_id for n in ev.output.get("not_asked") or []):
                out.append({"stage": "decide", "step": "not_asked", "reason": "past the question budget"})
        else:
            for op in ev.output.get("operations") or []:
                if op.get("question_id") == "q_" + item_id:
                    out.append({"stage": "approve", "step": "operation", "op_id": op.get("op_id"),
                                "destination": op.get("destination")})
    return out


def explain(top: dict[str, Any], mem: dict[str, Any] | None, extra: list[dict[str, Any]]) -> dict[str, Any]:
    row = mem or top
    trace = list(row.get("trace") or [])
    if mem is not None:
        trace.append({"stage": "merge", "step": "topic", "into": top["id"], "status": top.get("status"),
                      "score": top.get("score"), "reason": top.get("reason")})
    trace += extra
    rules = [e["rule"] for e in trace if e.get("step") == "rule"]
    return {
        "id": row["id"], "topic": top.get("topic"), "topic_item": top["id"], "merged": mem is not None,
        "title": row.get("title"), "source": row.get("source"), "status": top.get("status"),
        "keep": top.get("keep"), "score": top.get("score"), "reason": top.get("reason"), "rules": rules,
        "members": [m["id"] for m in top.get("members") or []], "trace": trace,
        "lines": [line(e) for e in trace],
    }


FILTER_KEYS = ("status", "source", "unit", "topic", "tag", "surface", "sensitivity", "route", "rule", "keep")


def matches(it: dict[str, Any], filters: list[tuple[str, str]], words: list[str]) -> bool:
    sig = it.get("signals") or {}
    for k, v in filters:
        vals: list[Any]
        if k == "tag":
            vals = sig.get("tags") or []
        elif k == "rule":
            vals = sig.get("rules") or []
        elif k in ("surface",):
            vals = [str(bool(sig.get("surface"))).lower()]
        elif k == "keep":
            vals = [str(bool(it.get("keep"))).lower()]
        elif k in ("sensitivity", "route"):
            vals = [sig.get(k)]
        else:
            vals = [it.get(k)]
        if not any(str(x).lower() == v.lower() for x in vals if x is not None):
            return False
    hay = f"{it.get('title') or ''}\n{it.get('text') or ''}".lower()
    return all(w.lower() in hay for w in words)


def parse_filter(text: str | None) -> tuple[list[tuple[str, str]], list[str]]:
    filters, words = [], []
    for part in (text or "").replace(",", " ").split():
        if "=" in part:
            k, v = part.split("=", 1)
            if k not in FILTER_KEYS:
                raise InputError("item.bad_filter", f"Unknown filter key {k!r}.", hint="docket help item list",
                                 details={"keys": list(FILTER_KEYS), "suggestion": closest(k, list(FILTER_KEYS))})
            filters.append((k, v))
        else:
            words.append(part)
    return filters, words
