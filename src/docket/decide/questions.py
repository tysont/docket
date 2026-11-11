"""Question generation (TD §10.1).

``build(rc, items, node)`` turns the items an ask node works on, the reconcile window and
the verification results into the ordered questions of the burst:

- ``finding``: one per retained item (``q_`` + item id), at or above the
  scoring floor, best score first, capped by the workflow's question budget;
  the rest are ``not_asked`` with their traces;
- ``reconcile``: one per elapsed owned block whose task is open (``r_`` +
  block key), when the workflow's ``questions.kinds`` lists ``reconcile``;
- ``verify``: one per open task that looks done (``v_`` + task key), when the
  kinds list ``verify``.

Kinds are ordered by ``decisions.order`` (reference: reconcile, then findings
by score, then verify). A question::

    {"id", "kind", "prompt", "evidence": [str], "options": [{"id", "label", "action",
     "destination", "defaults"}], "refused": [{"option", "action", "reason"}], "notes": [str],
     "requires": {option: [override|alternative]}, "subject": "<subject digest>",
     "item_id" | "block_key" | "task_id", "score"}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from docket.config.defaults import DEFAULT_OPTION_SET
from docket.config.digest import digest
from docket.decide import reconcile, verify
from docket.decide.options import Acceptor, finding_options, item_view, recommended_option
from docket.shapes.unexpected import unexpected
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

EVIDENCE_LIMIT = 3
EVIDENCE_CHARS = 160


@dataclass
class QuestionSet:
    questions: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    reconcile: list[dict[str, Any]] = field(default_factory=list)
    verify: list[dict[str, Any]] = field(default_factory=list)
    not_asked: list[dict[str, Any]] = field(default_factory=list)
    pending_refs: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, dict[str, Any]]:
        return {q["id"]: q for q in self.questions}

    def subjects(self) -> dict[str, dict[str, Any]]:
        """Question id -> its subject row (the finding's item, the reconcile or verify row)."""
        out: dict[str, dict[str, Any]] = {}
        for f in self.findings:
            out[f["question_id"]] = f["item"]
        for r in self.reconcile + self.verify:
            out[r["question_id"]] = r
        return out


def option_set(rc: "RunContext", name: str | None) -> list[dict[str, Any]]:
    """The finding options of the option set an ask node names."""
    sets = rc.ws.config.decisions.option_sets
    name = name or DEFAULT_OPTION_SET
    chosen = sets.get(name, {}).get("finding")
    if chosen is None:
        unexpected("decide.options", f"option set {name} has no finding options",
                   reading="use the default option set", extend=f"decisions.option_sets.{name}.finding")
        chosen = sets[DEFAULT_OPTION_SET]["finding"]
    return [o.model_dump() for o in chosen]


def item_subject(it: dict[str, Any]) -> str:
    """What counts as new activity on an item: its evidence, latest instant and members."""
    ev = [(e.get("item_id"), e.get("record_id"), e.get("text")) for e in it.get("evidence") or []]
    if not ev:
        ev = [(it.get("id"), it.get("record_id"), it.get("text") or it.get("title"))]
    members = sorted(m.get("id", "") for m in it.get("members") or [] if isinstance(m, dict))
    return digest({"evidence": ev, "at": it.get("at"), "members": members, "merged": it.get("merged") or []})


def evidence_summary(it: dict[str, Any]) -> list[str]:
    out = []
    for e in (it.get("evidence") or [])[:EVIDENCE_LIMIT]:
        text = " ".join(str(e.get("text") or "").split())
        if len(text) > EVIDENCE_CHARS:
            text = text[:EVIDENCE_CHARS - 1].rstrip() + "\u2026"
        out.append(f"[{e.get('source') or it.get('source')}] {text}")
    if not out and (it.get("text") or it.get("title")):
        out.append(f"[{it.get('source')}] {' '.join(str(it.get('text') or it.get('title')).split())[:EVIDENCE_CHARS]}")
    against = (it.get("review") or {}).get("against")
    if against:
        out.append(f"Against asking: {against}")
    return out


def select_asked(items: list[dict[str, Any]], floor: int, budget: int | None, must_surface: str = "ranked", *,
                 batch: set[str] | None = None, batch_budget: int | None = None,
                 surfaced: str | None = None,
                 floors: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(items to ask, items past a budget), ranked by score.

    A must-surface item sits at the floor by construction, so ranking alone cuts exactly the
    ones a rule insisted on. ``must_surface`` says what to do about that: ``ranked`` leaves
    them to their score, ``first`` asks them before the rest within the budget, ``bypass``
    asks them first and spends no budget on them (FR-SIG-05).

    ``batch`` names the options whose items are shown together. An item recommended for one
    (by the assess task, or by ``surfaced`` when a rule kept what the task did not) is ranked
    and cut in its own group against ``batch_budget``, so a list of things to acknowledge
    and the questions asked one by one do not crowd each other out.
    """
    def own_floor(it: dict[str, Any]) -> int:
        decision = (it.get("assessment") or {}).get("decision")
        return int((floors or {}).get(decision, floor)) if decision else floor

    kept = [it for it in items if it.get("keep") and int(it.get("score") or 0) >= own_floor(it)]
    kept.sort(key=lambda it: (-int(it.get("score") or 0), it["id"]))

    def is_must(it: dict[str, Any]) -> bool:
        return bool((it.get("signals") or {}).get("surface"))

    def in_batch(it: dict[str, Any]) -> bool:
        if not batch:
            return False
        a = it.get("assessment") or {}
        pick = surfaced if (is_must(it) and a.get("keep") is False) else a.get("decision")
        return pick in batch

    def cut(group: list[dict[str, Any]], cap: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if must_surface == "ranked":
            return (group, []) if cap is None else (group[:cap], group[cap:])
        must = [it for it in group if is_must(it)]
        rest = [it for it in group if not is_must(it)]
        if cap is None:
            return must + rest, []
        if must_surface == "first":
            ordered = must + rest
            return ordered[:cap], ordered[cap:]
        return must + rest[:cap], rest[cap:]

    single = [it for it in kept if not in_batch(it)]
    grouped = [it for it in kept if in_batch(it)]
    if must_surface != "ranked":  # a must-surface item leads, whichever group it is in
        lead = [it for it in kept if is_must(it)]
        a1, o1 = cut([it for it in single if not is_must(it)], budget)
        a2, o2 = cut([it for it in grouped if not is_must(it)], batch_budget)
        if must_surface == "first" and budget is not None:
            head = lead[:budget]
            room = budget - len(head)
            return head + a1[:room] + a2, lead[budget:] + a1[room:] + o1 + o2
        return lead + a1 + a2, o1 + o2
    a1, o1 = cut(single, budget)
    a2, o2 = cut(grouped, batch_budget)
    return a1 + a2, o1 + o2


def _findings(rc: "RunContext", items: list[dict[str, Any]], acceptor: Acceptor,
              notes_by_item: dict[str, dict[str, Any]], node: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                                                            list[dict[str, Any]]]:
    floor = int(((rc.ws.config.signals.get("scoring") or {}).get("floor")) or 0)
    opts = option_set(rc, node.ask)
    wq = node.questions
    asked, over = select_asked(items, floor, wq.budget, wq.must_surface,
                               batch={o["id"] for o in opts if o.get("batch")}, batch_budget=wq.batch_budget,
                               surfaced=rc.ws.config.decisions.recommend.surfaced,
                               floors=dict((rc.ws.config.signals.get("scoring") or {}).get("decision_floors") or {}))
    prompt_tpl = templates.parse(rc.ws.config.decisions.templates.get("finding_prompt", "{title}"))
    questions = []
    for it in asked:
        view = item_view(rc, it)
        offered, refused, defaults = finding_options(rc, it, opts, acceptor, view)
        notes = [f"Not offered: {r['option']}: {r['reason']}" for r in refused]
        ev = evidence_summary(it)
        vn = notes_by_item.get(it["id"])
        if vn:
            ev.append(f"Verification: {vn['ref']} is {vn.get('state') or 'unknown'}"
                      + (f", last actor {vn['last_actor']}" if vn.get("last_actor") else ""))
            if vn.get("reason"):
                notes.append(f"Looks done: {vn['reason']}")
        questions.append({
            "id": "q_" + it["id"], "kind": "finding", "item_id": it["id"], "key": view.get("key") or "",
            "score": int(it.get("score") or 0),
            "prompt": templates.render(prompt_tpl, view), "evidence": ev, "options": offered, "refused": refused,
            "notes": notes, "defaults": {k: v for k, v in defaults.items() if k in ("due", "est", "area", "forum")},
            "subject": item_subject(it),
            **({"recommended": rec} if (rec := recommended_option(it, offered, rc.ws.config.decisions.recommend)) else {}),
        })
    not_asked = [{"item_id": it["id"], "title": it.get("title", ""), "score": int(it.get("score") or 0),
                  "reason": "past the question budget", "trace": it.get("trace") or []} for it in over]
    return questions, [{"question_id": "q_" + it["id"], "item": it} for it in asked], not_asked


def build(rc: "RunContext", items: list[dict[str, Any]], node: Any) -> QuestionSet:
    """The questions of ask node ``node`` (its settings: the option set and what it asks)."""
    from docket.decide.store import open_tasks
    kinds = set(node.questions.kinds)
    qs = QuestionSet()
    tasks = open_tasks(rc) if kinds & {"reconcile", "verify"} else []
    tg = verify.targets(rc, items, tasks, verify_tasks="verify" in kinds)
    qs.pending_refs = verify.pending(rc, tg)
    acceptor = Acceptor(rc)
    try:
        by_kind: dict[str, list[dict[str, Any]]] = {"finding": [], "reconcile": [], "verify": []}
        if "finding" in kinds:
            notes = verify.item_notes(rc, tg) if not qs.pending_refs else {}
            by_kind["finding"], qs.findings, qs.not_asked = _findings(rc, items, acceptor, notes, node)
        if "reconcile" in kinds:
            for row in reconcile.candidates(rc, tasks):
                q = reconcile.question(rc, row)
                qs.reconcile.append({"question_id": q["id"], **row})
                by_kind["reconcile"].append(q)
        if "verify" in kinds and not qs.pending_refs:
            for row in verify.rows(rc, tg):
                q = verify.question(rc, row)
                qs.verify.append({"question_id": q["id"], **row})
                by_kind["verify"].append(q)
    finally:
        acceptor.close()
    order = list(dict.fromkeys(list(rc.ws.config.decisions.order) + ["reconcile", "finding", "verify"]))
    for k in order:
        qs.questions += by_kind.get(k, [])
    return qs


__all__ = ["build", "QuestionSet", "item_subject", "option_set"]
