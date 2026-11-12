"""``docket run diff``: what two runs of one workspace did differently (TD §5.10).

A run is read as it stands. A sealed or abandoned run is read from the artifacts it stored,
as they were built. An open run is evaluated against the workspace, as ``docket next`` does,
so answers given since the last artifact count; a node that is not current is read from its
stored artifact. Nothing is written.

Items are matched by topic. A question is matched by its item's topic, or by its own id when
it has no item (reconcile, covered and verify questions have stable ids). An operation is
matched by its destination, its verb and its item's topic, or the line, title or subject it
writes when it has no item.
"""
from __future__ import annotations

import json
import re
from typing import Any

from docket.store.runs import Run

COUNTS = ("read", "kept", "asked", "answered", "operations")


def _stored(run: Run) -> dict[str, dict[str, Any]]:
    """node -> {verb, data} of each node's latest stored artifact."""
    out: dict[str, dict[str, Any]] = {}
    root = run.dir / "artifacts"
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        latest = d / "latest"
        if not latest.is_file():
            continue
        p = d / f"{latest.read_text(encoding='utf-8').strip()}.json"
        if p.is_file():
            body = json.loads(p.read_text(encoding="utf-8"))
            out[d.name] = {"verb": body.get("verb"), "data": body.get("data") or {}}
    return out


def outputs(run: Run, rc: Any = None) -> dict[str, dict[str, Any]]:
    """node -> {verb, data}: what each node of the run handed on."""
    out = _stored(run)
    if rc is not None:
        for ev in rc.evaluate():
            if ev.state == "current" and ev.output is not None:
                out[ev.name] = {"verb": ev.verb, "data": ev.output}
    return out


def snapshot(run: Run, rc: Any = None) -> dict[str, Any]:
    """What the run read, kept, asked, answered and planned, keyed for matching."""
    from docket.engine.nodes import op_line
    outs = outputs(run, rc)
    by_verb: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(outs):
        by_verb.setdefault(outs[name]["verb"] or "", []).append(outs[name]["data"])
    read = sum(len(d.get("items") or []) for d in by_verb.get("read", []))
    items: dict[str, dict[str, Any]] = {}
    topic_of: dict[str, str] = {}
    for d in by_verb.get("merge", []):
        for it in d.get("items") or []:
            topic = str(it.get("topic") or it["id"])
            items[topic] = it
            topic_of[it["id"]] = topic
            for m in it.get("members") or []:
                topic_of.setdefault(m["id"], topic)
    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, dict[str, Any]] = {}
    for d in by_verb.get("ask", []):
        got = d.get("answers") or {}
        for q in d.get("questions") or []:
            key = topic_of.get(str(q.get("item_id") or ""), q["id"])
            questions[key] = q
            if q["id"] in got:
                answers[key] = got[q["id"]]
    ops: dict[tuple[str, str, str], dict[str, Any]] = {}
    outcome: dict[str, str] = {}
    for verb in ("apply", "write"):
        for d in by_verb.get(verb, []):
            for r in d.get("receipts") or []:
                outcome[r.get("op_id", "")] = r.get("outcome", "")
    for verb in ("plan", "write"):
        for d in by_verb.get(verb, []):
            for o in d.get("operations") or []:
                what = topic_of.get(str(o.get("item_id") or ""), "") or str(
                    o.get("line") or o.get("title") or o.get("subject") or o.get("op_id") or "")
                ops[(str(o.get("destination") or ""), str(o.get("verb") or ""), what)] = {
                    "destination": o.get("destination"), "verb": o.get("verb"), "text": _op_text(op_line(o), o),
                    "outcome": outcome.get(o.get("op_id", "")) or None}
    counts = {"read": read, "kept": sum(1 for it in items.values() if it.get("status") == "kept"),
              "asked": len(questions), "answered": len(answers), "operations": len(ops)}
    return {"counts": counts, "items": items, "questions": questions, "answers": answers, "ops": ops}


def _op_text(line: str, o: dict[str, Any]) -> str:
    """An operation in words, without the destination its group already names: ``add `x`
    (tasks.md under ## Inbox)``."""
    rest = line.removeprefix("- ").removeprefix(f"{o.get('destination')} ").removeprefix(f"{o.get('destination')}: ")
    m = re.fullmatch(r"\((.*?)\): (.*)", rest, re.S)
    return f"{m.group(2)} ({m.group(1)})" if m else rest


def _reason(it: dict[str, Any] | None) -> str:
    """Why an item stands where it does, from its trace."""
    if it is None:
        return "not read"
    if it.get("reason"):
        return f"{it.get('status')}: {it['reason']}"
    for step in reversed(it.get("trace") or []):
        if step.get("reason"):
            return f"{it.get('status')}: {step['reason']}"
        if step.get("rule") or step.get("rules"):
            return f"{it.get('status')}: rule {step.get('rule') or ', '.join(step['rules'])}"
    return f"{it.get('status')} at score {it.get('score')}"


def _title(*its: dict[str, Any] | None, q: dict[str, Any] | None = None) -> str:
    for it in its:
        if it and it.get("title"):
            return str(it["title"])[:90]
    return str((q or {}).get("prompt") or "")[:90]


def _answer(a: dict[str, Any] | None) -> str | None:
    if not a:
        return None
    over = a.get("overrides") or {}
    return str(a.get("option")) + (" (" + ", ".join(f"{k}={v}" for k, v in sorted(over.items())) + ")" if over else "")


def _bounded(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {"total": len(rows), "rows": rows[:limit]}


def compare(a: dict[str, Any], b: dict[str, Any], *, limit: int = 20) -> dict[str, Any]:
    counts = {k: {"a": a["counts"][k], "b": b["counts"][k], "change": b["counts"][k] - a["counts"][k]}
              for k in COUNTS}
    kept = []
    for topic in sorted(set(a["items"]) | set(b["items"])):
        ia, ib = a["items"].get(topic), b["items"].get(topic)
        ka, kb = bool(ia and ia.get("status") == "kept"), bool(ib and ib.get("status") == "kept")
        if ka != kb:
            kept.append({"topic": topic, "title": _title(ib, ia), "kept_in": "b" if kb else "a",
                         "a": {"status": (ia or {}).get("status"), "score": (ia or {}).get("score"), "reason": _reason(ia)},
                         "b": {"status": (ib or {}).get("status"), "score": (ib or {}).get("score"), "reason": _reason(ib)}})
    kept.sort(key=lambda r: (-max(int(r["a"]["score"] or 0), int(r["b"]["score"] or 0)), r["topic"]))
    moved = {r["topic"] for r in kept}
    changed = []
    for topic in sorted(set(a["items"]) & set(b["items"]) | set(a["questions"]) & set(b["questions"])):
        ia, ib = a["items"].get(topic), b["items"].get(topic)
        qa, qb = a["questions"].get(topic), b["questions"].get(topic)
        row: dict[str, Any] = {"topic": topic, "title": _title(ib, ia, q=qb or qa)}
        if ia and ib and ia.get("score") != ib.get("score") and topic not in moved:
            row["score"] = {"a": ia.get("score"), "b": ib.get("score")}
        if qa and qb and qa.get("recommended") != qb.get("recommended"):
            row["recommended"] = {"a": qa.get("recommended"), "b": qb.get("recommended")}
        ans_a, ans_b = _answer(a["answers"].get(topic)), _answer(b["answers"].get(topic))
        if qa and qb and ans_a != ans_b:
            row["answer"] = {"a": ans_a, "b": ans_b}
        if len(row) > 2:
            changed.append(row)

    def weight(r: dict[str, Any]) -> tuple[int, int, int, str]:
        s = r.get("score") or {}
        return (0 if "answer" in r else 1, 0 if "recommended" in r else 1,
                -abs(int(s.get("b") or 0) - int(s.get("a") or 0)), r["topic"])
    changed.sort(key=weight)
    dests: dict[str, dict[str, Any]] = {}
    for key in sorted(set(a["ops"]) | set(b["ops"])):
        oa, ob = a["ops"].get(key), b["ops"].get(key)
        d = dests.setdefault(key[0], {"destination": key[0], "a": 0, "b": 0, "only_a": [], "only_b": [],
                                      "outcome": []})
        d["a"] += oa is not None
        d["b"] += ob is not None
        if oa is None:
            d["only_b"].append(ob["text"])  # type: ignore[index]
        elif ob is None:
            d["only_a"].append(oa["text"])
        elif oa["outcome"] != ob["outcome"]:
            d["outcome"].append({"text": ob["text"], "a": oa["outcome"], "b": ob["outcome"]})
    ops = [d for d in dests.values() if d["only_a"] or d["only_b"] or d["outcome"]]
    ops.sort(key=lambda d: (-(len(d["only_a"]) + len(d["only_b"]) + len(d["outcome"])), d["destination"]))
    for d in ops:
        for k in ("only_a", "only_b", "outcome"):
            d[k] = _bounded(d[k], limit)
    return {"counts": counts, "kept": _bounded(kept, limit), "changed": _bounded(changed, limit),
            "operations": _bounded(ops, limit), "limit": limit,
            "same": not (kept or changed or ops or any(c["change"] for c in counts.values()))}


def text(head: dict[str, Any], diff: dict[str, Any]) -> str:
    """Grouped, bounded text: counts, kept in one run only, items that differ, operations."""
    def run_label(r: dict[str, Any]) -> str:
        return f"{r['run']} ({r['status']}" + (", sandbox" if r["sandbox"] else "") + \
            (f", graph {r['graph']}" if head["a"]["graph"] != head["b"]["graph"] else "") + ")"

    out = [f"A {run_label(head['a'])}", f"B {run_label(head['b'])}", "", f"{'counts':<12}{'A':>6}{'B':>6}"]
    for k in COUNTS:
        c = diff["counts"][k]
        out.append(f"  {k:<10}{c['a']:>6}{c['b']:>6}" + (f"   {c['change']:+d}" if c["change"] else ""))

    def heading(title: str, group: dict[str, Any]) -> None:
        shown = len(group["rows"])
        out.extend(["", f"{title} ({group['total']})" + (f", first {shown}" if shown < group["total"] else "")
                    + ":" if group["total"] else f"{title}: none"])

    heading("kept in one run only", diff["kept"])
    for r in diff["kept"]["rows"]:
        lost = "a" if r["kept_in"] == "b" else "b"
        out.append(f"  kept in {r['kept_in'].upper()} only: {r['title']}")
        score = f" (score {r[lost]['score']})" if r[lost]["score"] is not None else ""
        out.append(f"    {lost.upper()}: {r[lost]['reason']}{score}; {r['kept_in'].upper()}: kept at score "
                   f"{r[r['kept_in']]['score']}")
    heading("items that differ", diff["changed"])
    for r in diff["changed"]["rows"]:
        parts = [f"{k} {r[k]['a']} -> {r[k]['b']}" for k in ("answer", "recommended", "score") if k in r]
        out.append(f"  {r['title']}: " + "; ".join(parts))
    heading("operations that differ", diff["operations"])
    for d in diff["operations"]["rows"]:
        out.append(f"  {d['destination']}: {d['a']} -> {d['b']}")
        for k, mark in (("only_a", "only in A"), ("only_b", "only in B")):
            for t in d[k]["rows"]:
                out.append(f"    {mark}: {t}")
            if d[k]["total"] > len(d[k]["rows"]):
                out.append(f"    ... {d[k]['total'] - len(d[k]['rows'])} more {mark}")
        for o in d["outcome"]["rows"]:
            out.append(f"    {o['a']} -> {o['b']}: {o['text']}")
    if diff["same"]:
        out += ["", "the two runs read, kept, asked, answered and planned the same"]
    return "\n".join(out)
