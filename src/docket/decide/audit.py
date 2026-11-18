"""Audit questions (TD §10.7): does the operator take what an agenda audit proposes?

An ask node that lists ``audit`` in ``questions.kinds`` names the ``judge: audit`` node whose
proposals it asks about and the agenda destination they write (``questions.audit``). Each
proposal that changes something (an entry reworded or dropped, or a topic added) is one
question, about one forum. Its details carry the whole proposal: the current entries with
their verdicts, the additions (A1, A2, ...), the commitments, the focus, what the audit could
not tell, and the list the agenda would hold.

Option kinds are fixed and labels come from ``decisions.audit.labels``: ``accept`` applies
every proposed change, ``additions`` only adds the missing topics and leaves the entries as
they are, ``skip`` changes nothing. An answer may name the changes to leave out:
``--keep N2,N5`` keeps entries the audit would drop or reword, and ``--without A1`` leaves an
addition out. The question's id derives from the forum and the pack the audit read, so the
same note and the same ledger items keep it; a ``skip``, and an answer that leaves every
change out, is remembered at a durable seal (a ``skip`` disposition keyed by the question
id), and the same proposal is not asked again until the note or the items change.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from docket.cli.errors import InputError
from docket.config.digest import digest
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

OPTIONS = ("accept", "additions", "skip")
LABELS = {
    "accept": "Apply every change ({changes})",
    "additions": "Only add the missing topics",
    "skip": "Change nothing",
}
PROMPT = "Update the {forum.name} agenda as its audit proposes ({changes})?"
ACTIONS = {"accept": "audit.accept", "additions": "audit.additions", "skip": "audit.skip"}
_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def question_id(audit: dict[str, Any]) -> str:
    return "u_" + digest(["audit", audit["forum"], audit["digest"]])[:16]


def proposal(audit: dict[str, Any], capacity: int | None = None) -> dict[str, Any]:
    """The proposal of one audit as a question shows it and a write applies it."""
    row = audit.get("row") or {}
    verdicts = {str(e.get("line")): e for e in row.get("entries") or []}
    links = {r["link"]: r["key"] for r in audit.get("ledger") or [] if r.get("link")}
    entries = []
    for e in audit.get("entries") or []:
        v = verdicts.get(e["line"]) or {}
        verdict = v.get("verdict") or "keep"
        new = " ".join(str(v.get("text") or "").split()) if verdict == "reword" else ""
        entries.append({"line": e["line"], "text": e["text"], "verdict": verdict,
                        **({"new": _MARK.sub("", new)} if new else {}), "why": v.get("why") or ""})
    additions = []
    for i, a in enumerate(row.get("add") or [], 1):
        add = {"id": f"A{i}", "text": _MARK.sub("", " ".join(str(a.get("text") or "").split())),
               "why": a.get("why") or ""}
        for k in ("link", "evidence"):
            if a.get(k):
                add[k] = a[k]
        if a.get("link") in links:
            add["key"] = links[a["link"]]
        additions.append(add)
    return {"entries": entries, "additions": additions, "commitments": list(row.get("commitments") or []),
            "focus": row.get("focus") or "", "unknown": list(row.get("unknown") or []),
            "proposed": proposed_list(entries, additions, capacity)}


def proposed_list(entries: list[dict[str, Any]], additions: list[dict[str, Any]], capacity: int | None) -> list[str]:
    """The entries the agenda section would hold with every change applied, additions within capacity."""
    out = []
    for e in entries:
        if e["verdict"] == "drop":
            continue
        if e["verdict"] == "reword" and e.get("new"):
            m = _MARK.match(e["text"])
            out.append((m.group(0) if m else "- ") + e["new"])
        else:
            out.append(e["text"])
    room = None if capacity is None else max(0, capacity - len(out))
    for a in additions if room is None else additions[:room]:
        out.append(f"- {a['text']}")
    return out


def counts(p: dict[str, Any]) -> dict[str, int]:
    return {"reword": sum(1 for e in p["entries"] if e["verdict"] == "reword" and e.get("new")),
            "drop": sum(1 for e in p["entries"] if e["verdict"] == "drop"), "add": len(p["additions"])}


def describe(c: dict[str, int]) -> str:
    parts = [f"{c['reword']} reworded" if c["reword"] else "", f"{c['drop']} dropped" if c["drop"] else "",
             f"{c['add']} added" if c["add"] else ""]
    return ", ".join(p for p in parts if p) or "no change"


def remembered(rc: "RunContext") -> set[str]:
    """Question ids an earlier durable seal recorded as skipped."""
    return {r["topic_id"] for r in rc.run.store.db.execute(
        "SELECT topic_id FROM dispositions WHERE decision='skip' AND topic_id LIKE 'u\\_%' ESCAPE '\\' "
        "AND (run_id IS NULL OR run_id<>?)", (rc.run.id,))}


def candidates(rc: "RunContext", node: Any) -> list[dict[str, Any]]:
    """The rows the ask node ``node`` asks about: one per audited forum whose proposal changes
    something and was not skipped before."""
    aq = node.questions.audit
    if aq is None:
        return []
    ev = rc.eval_or_none(aq.from_)
    audits = list(((ev.output if ev is not None and ev.state == "current" else None) or {}).get("audits") or [])
    dcfg = rc.ws.config.destinations.get(aq.to)
    cap = getattr(dcfg, "capacity", None)
    skip = remembered(rc)
    out = []
    for a in audits:
        p = proposal(a, cap)
        c = counts(p)
        if not any(c.values()):
            continue
        row = {"forum": a["forum"], "forum_name": a["forum_name"], "person": a.get("person"), "path": a["path"],
               "destination": aq.to, "meetings": a.get("meetings") or {}, "audit": question_id(a), **p}
        if row["audit"] not in skip:
            out.append(row)
    return out


def context(row: dict[str, Any]) -> dict[str, Any]:
    c = counts(row)
    person = row.get("person") or {}
    return {"forum": {"id": row["forum"], "name": row["forum_name"]},
            "person": {"id": person.get("id") or "", "name": person.get("name") or ""},
            "changes": describe(c), "counts": c}


def question(rc: "RunContext", row: dict[str, Any]) -> dict[str, Any]:
    cfg = rc.ws.config.decisions
    ctx = context(row)
    labels = {**LABELS, **cfg.audit.labels}
    opts, refused = [], []
    for oid in OPTIONS:
        if oid == "additions" and not row["additions"]:
            refused.append({"option": oid, "action": ACTIONS[oid], "reason": "the audit proposes no topic to add"})
            continue
        opts.append({"id": oid, "label": templates.render(labels[oid], ctx), "action": ACTIONS[oid], "defaults": {}})
    details = {k: row[k] for k in ("forum", "forum_name", "person", "path", "meetings", "focus", "entries",
                                   "additions", "commitments", "unknown", "proposed")}
    subject = digest({k: row[k] for k in ("forum", "audit", "entries", "additions")})
    notes = [f"Not offered: {r['option']}: {r['reason']}" for r in refused]
    return {"id": row["audit"], "kind": "audit",
            "prompt": templates.render(cfg.templates.get("audit_prompt", PROMPT), ctx),
            "evidence": [row["focus"]] if row["focus"] else [], "options": opts, "refused": refused, "notes": notes,
            "recommended": "accept", "subject": subject, "audit": details}


# --- answers ----------------------------------------------------------------------------


def parse_ids(values: Any, prefix: str, flag: str) -> list[str]:
    """``--keep N2,N5`` (repeatable) as ``["N2", "N5"]``; each id is the prefix and a number."""
    out: list[str] = []
    for v in values if isinstance(values, list) else [values]:
        for part in str(v).replace(" ", ",").split(","):
            p = part.strip().upper()
            if not p:
                continue
            if not re.fullmatch(prefix + r"[1-9]\d*", p):
                raise InputError("input.bad_override", f"{flag} {part!r} is not an id such as {prefix}1",
                                 hint=f"{flag} {prefix}1,{prefix}2")
            if p not in out:
                out.append(p)
    return out


def check_overrides(q: dict[str, Any], option: str, ov: dict[str, Any]) -> None:
    """``--keep`` and ``--without`` name changes the question's proposal makes, and apply only
    where the option makes them: ``--keep`` with accept, ``--without`` with accept or additions."""
    keep, without = ov.get("keep") or [], ov.get("without") or []
    if not keep and not without:
        return
    if q.get("kind") != "audit":
        raise InputError("input.bad_override", "--keep and --without apply to audit questions only.",
                         hint=f"docket ask show {q['id']}")
    if option == "skip" or (keep and option == "additions"):
        flag = "--keep" if keep else "--without"
        raise InputError("input.bad_override", f"{flag} has nothing to leave out with {option}: "
                         + ("skip changes nothing" if option == "skip" else "additions leaves every entry as it is"),
                         hint=f"docket ask answer {q['id']} accept {flag} ...")
    a = q.get("audit") or {}
    changed = [e["line"] for e in a.get("entries") or [] if e["verdict"] == "drop"
               or (e["verdict"] == "reword" and e.get("new"))]
    adds = [x["id"] for x in a.get("additions") or []]
    for flag, given, allowed, what in (("--keep", keep, changed, "entries the audit drops or rewords"),
                                       ("--without", without, adds, "additions")):
        bad = [x for x in given if x not in allowed]
        if bad:
            raise InputError("input.bad_override", f"{flag} names {', '.join(bad)}, which the proposal does not "
                             f"change; its {what}: {', '.join(allowed) or 'none'}",
                             hint=f"docket ask show {q['id']}", details={"allowed": allowed, "given": given})


def changes(row: dict[str, Any], answer: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                                                  list[dict[str, Any]]]:
    """(entries to reword, entries to drop, additions) an answer applies, in that order."""
    opt = answer.get("option")
    ov = answer.get("overrides") or {}
    keep, without = set(ov.get("keep") or []), set(ov.get("without") or [])
    if opt not in ("accept", "additions"):
        return [], [], []
    adds = [a for a in row.get("additions") or [] if a["id"] not in without]
    if opt == "additions":
        return [], [], adds
    entries = [e for e in row.get("entries") or [] if e["line"] not in keep]
    return ([e for e in entries if e["verdict"] == "reword" and e.get("new")],
            [e for e in entries if e["verdict"] == "drop"], adds)


def effect(row: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """What an answer would change, for the answer's echo."""
    reworded, dropped, added = changes(row, answer)
    return {"reword": [e["line"] for e in reworded], "drop": [e["line"] for e in dropped],
            "add": [a["id"] for a in added], "path": row["path"]}


def dispositions(run_id: str, decide: dict[str, Any] | None) -> list[Any]:
    """The skips a durable seal remembers: an audit question answered ``skip``, or answered so that
    nothing is left to change. Keyed by the question id."""
    from docket.seal.dispositions import Disposition
    decide = decide or {}
    answers = decide.get("answers") or {}
    out = []
    for r in decide.get("audit") or []:
        ans = answers.get(r.get("question_id"))
        if not ans:
            continue
        if ans.get("option") == "skip" or not any(changes(r, ans)):
            out.append(Disposition(r["question_id"], "skip", run_id, note=f"agenda audit of {r['forum_name']}: "
                                   f"{ans.get('option')}"))
    return out


__all__ = ["candidates", "question", "check_overrides", "changes", "effect", "dispositions", "proposal",
           "question_id", "OPTIONS", "LABELS"]
