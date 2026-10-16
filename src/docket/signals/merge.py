"""Merge, suppression and coverage (TD §7.2, §7.3, §8.3, §8.4; FR-SIG-05, 07, 08, 09).

``build_merge(inputs)`` is a pure function of its inputs; the merge stage and
``docket config test`` both call it.

1. Rules (``docket.signals.engine``) and scores (``docket.signals.scoring``) per item.
2. Grouping: items sharing an identity key (a reference, then a real thread
   key, then an agent-assigned topic) form one topic. The topic id is the group's
   strongest key: its first reference, else its thread, else the agent topic, else
   the item id. The representative is the member with the strongest assessment
   (must-surface first, then score, then id); the group unions evidence, sources,
   tags and reasons, and each member's trace records the merge.
3. Group status, in order: ``dropped`` when every member was dismissed by a rule;
   ``suppressed`` by a live disposition (``dismiss`` until a date, ``watch`` with no
   activity since it was recorded, ``done``) or because the latest record is the
   operator's (``answered``); ``tracked`` when an open task in a task-store
   destination names one of the topic's references or its URL (routed to task
   verification, not a finding); ``dropped`` when an agent assessed it and kept
   nothing; then floors, caps and budgets.
4. Coverage: every ``expect_in`` identifier missing from its home source's
   records becomes a coverage note naming the item. The home source is not
   marked incomplete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from docket.signals.engine import SignalEnv, at_key, evaluate, instant, needs_agent, references
from docket.signals.scoring import decide_keep, rank_key, score_item

SCHEMA = "merge/1"


@dataclass
class MergeInputs:
    items: list[dict[str, Any]]                         # mapped items (collect), with assess output fields
    env: SignalEnv
    agent: dict[str, dict[str, Any]] = field(default_factory=dict)   # item id -> accepted assess row
    dispositions: list[dict[str, Any]] = field(default_factory=list)  # rows, each with ``recorded_at``
    task_stores: list[dict[str, Any]] = field(default_factory=list)   # {destination, path, open: [lines]}
    sources: dict[str, Any] = field(default_factory=dict)            # collect outcomes per source
    run_date: str | None = None
    run_id: str | None = None


# --- grouping ----------------------------------------------------------------------


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _strength(it: dict[str, Any]) -> tuple[int, int, str]:
    return (0 if it["signals"]["surface"] else 1, -int(it["score"]), it["id"])


def _topic(members: list[dict[str, Any]], patterns: list[str]) -> tuple[str, str]:
    order = {p: i for i, p in enumerate(patterns)}
    refs = sorted({k for m in members for k in m["keys"] if k.startswith("ref:")},
                  key=lambda k: (order.get(k.split(":", 2)[1], len(order)), k))
    if refs:
        return refs[0], "reference"
    threads = sorted({k for m in members for k in m["keys"] if k.startswith("thread:")})
    if threads:
        return threads[0], "thread"
    agents = sorted({k for m in members for k in m["keys"] if k.startswith("agent:")})
    if agents:
        return agents[0], "agent"
    return "item:" + min(m["id"] for m in members), "item"


def group(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    uf = _UF()
    for it in items:
        uf.find("i:" + it["id"])
        for k in it["keys"]:
            uf.union("i:" + it["id"], k)
    out: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        out.setdefault(uf.find("i:" + it["id"]), []).append(it)
    return [sorted(g, key=_strength) for _, g in sorted(out.items())]


# --- suppression ---------------------------------------------------------------------


def _disposition(group_ids: set[str], latest: Any, rows: list[dict[str, Any]], run_date: str | None,
                 run_id: str | None) -> dict[str, Any] | None:
    for row in rows:
        if row.get("run_id") == run_id or (row.get("topic_id") not in group_ids and row.get("item") not in group_ids):
            continue
        dec = row.get("decision")
        base = {"decision": dec, "run": row.get("run_id"), "until": row.get("until"), "note": row.get("note"),
                "recorded_at": row.get("recorded_at")}
        if dec == "done":
            return {**base, "reason": "disposition: done"}
        if dec == "dismiss":
            until = row.get("until")
            if until is None or run_date is None or run_date < str(until)[:10]:
                return {**base, "reason": "disposition: dismiss" + (f" until {until}" if until else "")}
            continue
        if dec == "watch":
            since = instant(row.get("recorded_at"))
            if since is None or latest is None or latest <= since:
                return {**base, "reason": "disposition: watch, no new activity"}
            continue
    return None


def _tracked(g: dict[str, Any], stores: list[dict[str, Any]]) -> dict[str, Any] | None:
    needles = [r["value"] for r in g["references"]] + ([g["url"]] if g.get("url") else [])
    needles = [n for n in needles if n]
    for st in stores:
        for line in st.get("open", []):
            for n in needles:
                if n in line:
                    return {"destination": st["destination"], "path": st.get("path"), "line": line, "match": n}
    return None


# --- coverage --------------------------------------------------------------------------


def coverage(items: list[dict[str, Any]], inp: MergeInputs) -> list[dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    home_vals: dict[str, set[str]] = {}
    home_text: dict[str, str] = {}

    def values(src: str) -> set[str]:
        if src not in home_vals:
            rows = by_source.get(src, [])
            home_vals[src] = {r["value"] for it in rows for r in references(it, inp.env.cfg)} | \
                {str(it.get("identity") or "") for it in rows}
            home_text[src] = "\n".join(f"{it.get('title') or ''} {it.get('text') or ''} {it.get('url') or ''}"
                                       for it in rows)
        return home_vals[src]

    notes: dict[tuple[str, str], dict[str, Any]] = {}
    for it in items:
        for exp in it["signals"]["expect_in"]:
            home = exp["source"]
            if home == it["source"]:
                continue
            for ref in it["references"]:
                if exp.get("reference") and ref["pattern"] != exp["reference"]:
                    continue
                v = ref["value"]
                if v in values(home) or v in home_text[home] or (home, v) in notes:
                    continue
                outcome = (inp.sources.get(home) or {}).get("outcome") or "not collected"
                notes[(home, v)] = {"home": home, "pattern": ref["pattern"], "value": v, "item_id": it["id"],
                                    "source": it["source"], "title": it.get("title") or "", "rule": exp.get("rule"),
                                    "home_outcome": outcome,
                                    "note": f"{it['source']} names {v}, which {home} did not return "
                                            f"(home outcome: {outcome})"}
                it["trace"].append({"stage": "merge", "step": "coverage_miss", "home": home, "value": v,
                                    "rule": exp.get("rule"), "home_outcome": outcome})
    return [notes[k] for k in sorted(notes)]


# --- the merge -------------------------------------------------------------------------


def _member_view(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if k not in ("members",)}


def build_merge(inp: MergeInputs) -> dict[str, Any]:
    env = inp.env
    sc, budgets = env.cfg.scoring, env.cfg.effective_budgets
    items = evaluate(sorted(inp.items, key=lambda x: x["id"]), env)
    for it in items:
        row = inp.agent.get(it["id"]) if needs_agent(it) else None
        agent = None
        if not needs_agent(it):
            why = "rules_only" if it["signals"]["assess"] == "rules_only" else "dismissed"
            it["trace"].append({"stage": "assess", "step": why, **(
                {"rules": it["signals"]["assess_rules"]} if why == "rules_only" else
                {"rules": [d["rule"] for d in it["signals"]["dismiss"]]})})
        elif row is None:
            it["agent_pending"] = True
            it["trace"].append({"stage": "assess", "step": "agent_pending",
                                "reason": "no accepted assess result for this item"})
        else:
            agent = int(row.get("score") or 0)
            it["agent"] = {"keep": bool(row.get("keep")), "score": agent,
                           **({"topic": row["topic"]} if row.get("topic") else {})}
            it["trace"].append({"stage": "assess", "step": "agent", "keep": bool(row.get("keep")), "score": agent})
            if row.get("topic") and not it["keys"]:
                it["keys"].append(f"agent:{row['topic']}")
        score_item(it, sc, agent)
        it["keep"], it["status"], it["reason"] = False, "pending", None
    notes = coverage(items, inp)

    groups_out: list[dict[str, Any]] = []
    merged: dict[str, str] = {}
    for members in group(items):
        rep = dict(members[0])
        topic, basis = _topic(members, list(env.cfg.references))
        ids = [m["id"] for m in members]
        rep["topic"], rep["topic_basis"] = topic, basis
        rep["sources"] = sorted({m["source"] for m in members})
        rep["evidence"] = [{"item_id": m["id"], "source": m["source"], "record_id": m.get("record_id"),
                            "text": m.get("text") or m.get("title") or ""} for m in members]
        sig = dict(rep["signals"])
        sig["tags"] = sorted({t for m in members for t in m["signals"]["tags"]})
        sig["reasons"] = list(dict.fromkeys(r for m in members for r in m["signals"]["reasons"]))
        sig["surface"] = any(m["signals"]["surface"] for m in members)
        sig["surface_rules"] = list(dict.fromkeys(r for m in members for r in m["signals"]["surface_rules"]))
        for k in ("route", "area", "sensitivity", "deadline"):
            if sig[k] is None:
                sig[k] = next((m["signals"][k] for m in members if m["signals"][k] is not None), None)
        rep["signals"] = sig
        rep["references"] = list({(r["pattern"], r["value"]): r for m in members for r in m["references"]}.values())
        rep["trace"] = list(rep["trace"])
        if len(members) > 1:
            rep["members"] = [_member_view(m) for m in members[1:]]
            rep["trace"].append({"stage": "merge", "step": "merged", "topic": topic, "basis": basis,
                                 "members": ids[1:], "keys": sorted({k for m in members for k in m["keys"]})})
            for m in members[1:]:
                merged[m["id"]] = rep["id"]
                m["trace"].append({"stage": "merge", "step": "merged_into", "into": rep["id"], "topic": topic})
        else:
            rep["members"] = []
        latest = max(members, key=at_key)
        latest_at = max((instant(m.get("at")) for m in members if instant(m.get("at"))), default=None)
        group_ids = set(ids) | {topic}
        if all(m["signals"]["dismissed"] for m in members):
            d = rep["signals"]["dismiss"][0]
            rep.update(keep=False, status="dropped", reason=f"dismissed: {d['reason']}")
            rep["trace"].append({"stage": "merge", "step": "dismissed", "rules": [x["rule"] for x in sig["dismiss"]],
                                 "reason": d["reason"]})
        elif (disp := _disposition(group_ids, latest_at, inp.dispositions, inp.run_date, inp.run_id)) is not None:
            rep.update(keep=False, status="suppressed", reason=disp["reason"], suppressed=disp)
            rep["trace"].append({"stage": "merge", "step": "suppressed", **disp})
        elif latest["sender"]["is_me"]:
            sup = {"reason": "answered", "item": latest["id"]}
            rep.update(keep=False, status="suppressed", reason="answered: the latest record is the operator's",
                       suppressed=sup)
            rep["trace"].append({"stage": "merge", "step": "suppressed", **sup})
        elif (tr := _tracked(rep, inp.task_stores)) is not None:
            rep.update(keep=False, status="tracked", reason=f"tracked in {tr['destination']}", tracked=tr)
            rep["signals"]["route"] = "verify"
            rep["trace"].append({"stage": "merge", "step": "tracked", **tr})
        else:
            assessed = [m for m in members if m.get("agent")]
            if assessed and not any(m["agent"]["keep"] for m in assessed) and not sig["surface"]:
                rep.update(keep=False, status="dropped", reason="assess: the agent kept nothing")
                rep["trace"].append({"stage": "merge", "step": "agent_drop", "members": [m["id"] for m in assessed]})
        groups_out.append(rep)

    decide_keep([g for g in groups_out if g["status"] == "pending"], sc, budgets)
    groups_out.sort(key=rank_key)
    from docket.items.model import normalize
    out_items = [normalize(g) for g in groups_out]
    counts = {s: sum(1 for g in out_items if g["status"] == s) for s in ("kept", "dropped", "suppressed", "tracked")}
    counts.update(items=len(items), topics=len(out_items), merged=len(merged),
                  agent_pending=sum(1 for it in items if it.get("agent_pending")))
    return {
        "schema": SCHEMA,
        "items": out_items,
        "merged": dict(sorted(merged.items())),
        "coverage_notes": notes,
        "tracked": [{"item_id": g["id"], "topic": g["topic"], **g["tracked"]} for g in out_items if g.get("tracked")],
        "suppressed": [{"item_id": g["id"], "topic": g["topic"], **g["suppressed"]}
                       for g in out_items if g.get("suppressed")],
        "counts": counts,
        "scoring": sc.model_dump(mode="json", exclude={"budget"}),
        "budgets": budgets.model_dump(mode="json"),
    }
