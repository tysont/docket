"""Answers (TD §10.3, FR-DEC-04, FR-DEC-06).

Answers live in the run's ``answers.json``, keyed by question id. Question ids
derive from the subject (``q_`` + item id, ``r_`` + block key, ``v_`` + task
key), so an answer binds to identity, not position. Each stored answer carries
the digest of its subject as it was when answered (``subject``). On refresh an
answer whose subject digest is unchanged stays bound; a changed subject's
answer becomes ``previous`` and the question is asked again showing it.

A stored answer::

    {"kind": "finding", "option": "task", "action": "task.add", "destination": "backlog",
     "overrides": {"due": "2026-10-19", "est": 60, "when": "event:...", "title": "...",
                   "action": "...", "links": ["..."], "area": "...", "forum": "<forum id>",
                   "merge_with": ["q_..."], "note": "..."},
     "input": "<the operator's words for the option>", "subject": "<subject digest>",
     "previous": {...}}                  # the unbound answer this one replaced, when any

``merge_with`` folds other finding questions into this answer's action; each
of them is stored as answered by merge::

    {"kind": "finding", "option": "merged", "action": "merged", "merged_into": "q_...", "subject": "..."}
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from docket.cli.errors import InputError

if TYPE_CHECKING:
    from docket.engine.context import RunContext

OVERRIDES = ("due", "est", "when", "title", "action", "links", "area", "forum", "merge_with", "note")
MERGED = "merged"


class Answer(BaseModel):
    option: str
    action: str | None = None
    kind: str | None = None
    destination: str | None = None
    overrides: dict[str, Any] = {}
    digest: str | None = None
    input: str | None = None
    subject: str | None = None
    merged_into: str | None = None
    previous: dict[str, Any] | None = None


# --- normalization ---------------------------------------------------------------------


def _text(v: Any, name: str) -> str:
    s = " ".join(str(v).split())
    if not s:
        raise InputError("input.bad_argument", f"--{name.replace('_', '-')} is empty")
    return s


def resolve_forum(rc: "RunContext", text: str) -> str:
    forums = rc.ws.config.profile.forums
    low = " ".join(str(text).lower().split())
    tiers = [
        [f for f in forums if f.id.lower() == low],
        [f for f in forums if (f.name or "").lower() == low],
        [f for f in forums if f.id.lower().startswith(low) or (f.name or "").lower().startswith(low)],
    ]
    for hits in tiers:
        if len(hits) == 1:
            return hits[0].id
        if len(hits) > 1:
            raise InputError("input.ambiguous_option", f"--forum {text!r} matches {len(hits)} forums",
                             hint="docket config explain profile.forums",
                             details={"candidates": [{"id": f.id, "name": f.name} for f in hits]})
    raise InputError("input.unknown_forum", f"--forum {text!r} names no forum of the profile",
                     hint="docket config explain profile.forums",
                     details={"forums": [{"id": f.id, "name": f.name} for f in forums]})


def resolve_area(rc: "RunContext", text: str) -> str:
    s = _text(text, "area")
    for t in rc.ws.config.profile.topics:
        if t.area and t.area.casefold() == s.casefold():
            return t.area
    return s


def normalize_overrides(rc: "RunContext", raw: dict[str, Any], ref: date) -> dict[str, Any]:
    """Every override in normalized form; raises the input error of the first bad value."""
    from docket.plan.windows import normalize_when
    from docket.text.dates import normalize_date
    from docket.text.durations import parse_duration
    out: dict[str, Any] = {}
    unknown = sorted(set(raw) - set(OVERRIDES) - {"link"})
    if unknown:
        raise InputError("input.bad_argument", f"unknown answer field(s): {', '.join(unknown)}",
                         details={"fields": list(OVERRIDES)})
    if raw.get("due") not in (None, ""):
        out["due"] = normalize_date(str(raw["due"]), ref)
    if raw.get("est") not in (None, ""):
        out["est"] = parse_duration(raw["est"])
    if raw.get("when") not in (None, ""):
        out["when"] = normalize_when(str(raw["when"]), ref)
    for k in ("title", "action", "note"):
        if raw.get(k) not in (None, ""):
            out[k] = _text(raw[k], k)
    links: list[str] = []
    for v in [raw.get("links"), raw.get("link")]:
        for x in (v if isinstance(v, list) else [v] if v not in (None, "") else []):
            s = _text(x, "link")
            if s not in links:
                links.append(s)
    if links:
        out["links"] = links
    if raw.get("area") not in (None, ""):
        out["area"] = resolve_area(rc, raw["area"])
    if raw.get("forum") not in (None, ""):
        out["forum"] = resolve_forum(rc, str(raw["forum"]))
    mw = raw.get("merge_with")
    merge = [str(x).strip() for x in (mw if isinstance(mw, list) else [mw] if mw not in (None, "") else [])]
    if merge:
        out["merge_with"] = list(dict.fromkeys(m for m in merge if m))
    return out


def command_args(overrides: dict[str, Any]) -> list[str]:
    """The overrides as the canonical ``docket ask answer`` flags (the normalized echo)."""
    from docket.text.durations import format_duration
    out: list[str] = []
    for k in OVERRIDES:
        if k not in overrides:
            continue
        v = overrides[k]
        flag = "--" + k.replace("_", "-")
        if k == "links":
            for x in v:
                out += ["--link", x]
        elif k == "merge_with":
            for x in v:
                out += ["--merge-with", x]
        elif k == "est":
            out += [flag, format_duration(v)]
        else:
            out += [flag, str(v)]
    return out


def resolve_merge_targets(merge: list[str], qid: str, questions: dict[str, dict[str, Any]],
                          answers: dict[str, Any]) -> list[str]:
    """Exact question ids, or unique prefixes of them; each must be another finding question."""
    out = []
    for m in merge:
        hit = [m] if m in questions else [k for k in questions if k.startswith(m)]
        if len(hit) != 1:
            raise InputError("input.bad_merge", f"--merge-with {m!r} names " + ("no question" if not hit else
                             f"{len(hit)} questions"), hint="docket ask list",
                             details={"candidates": hit[:20]})
        t = hit[0]
        q = questions[t]
        if t == qid:
            raise InputError("input.bad_merge", "A question cannot merge with itself.", hint="docket ask list")
        if q["kind"] != "finding" or questions[qid]["kind"] != "finding":
            raise InputError("input.bad_merge", "Only finding questions merge.", hint="docket ask list",
                             details={"question": t, "kind": q["kind"]})
        held = answers.get(t) or {}
        if (held.get("overrides") or {}).get("merge_with"):
            raise InputError("input.bad_merge", f"{t} already absorbs other questions; merge into it instead.",
                             hint=f"docket ask answer {t} <option> --merge-with {qid}",
                             details={"question": t, "merge_with": held["overrides"]["merge_with"]})
        out.append(t)
    return out


# --- binding ------------------------------------------------------------------------------


def bind(questions: list[dict[str, Any]], stored: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(bound answers, previous answers) for the current questions (FR-DEC-06).

    An answer is bound when its subject digest equals the question's (or it predates subject
    digests) and its option is still offered. An answer by merge is bound when the absorbing
    answer is bound and still lists it.
    """
    qs = {q["id"]: q for q in questions}
    bound: dict[str, Any] = {}
    previous: dict[str, Any] = {}
    for qid, q in qs.items():
        a = stored.get(qid)
        if not isinstance(a, dict) or a.get("option") == MERGED:
            continue
        same = a.get("subject") in (None, q.get("subject"))
        offered = any(o["id"] == a.get("option") for o in q["options"])
        if same and offered:
            bound[qid] = a
        else:
            previous[qid] = a
    for qid, q in qs.items():
        a = stored.get(qid)
        if not isinstance(a, dict) or a.get("option") != MERGED:
            continue
        host = bound.get(a.get("merged_into") or "")
        if host and qid in ((host.get("overrides") or {}).get("merge_with") or []) \
                and a.get("subject") in (None, q.get("subject")):
            bound[qid] = a
        else:
            previous[qid] = a
    return bound, previous


def with_answer(stored: dict[str, Any], q: dict[str, Any], answer: dict[str, Any], questions: dict[str, dict[str, Any]],
                previous: dict[str, Any]) -> dict[str, Any]:
    """The new answers map after recording ``answer`` for ``q``, with merges applied."""
    out = dict(stored)
    qid = q["id"]
    old = out.get(qid)
    if isinstance(old, dict) and qid in previous:
        answer = {**answer, "previous": {k: v for k, v in old.items() if k != "previous"}}
    # questions this one absorbed before and no longer lists go back to unanswered
    keep = set((answer.get("overrides") or {}).get("merge_with") or [])
    for k, v in list(out.items()):
        if isinstance(v, dict) and v.get("merged_into") == qid and k not in keep:
            del out[k]
    out[qid] = answer
    for t in keep:
        out[t] = {"kind": "finding", "option": MERGED, "action": MERGED, "merged_into": qid, "overrides": {},
                  "subject": questions[t].get("subject")}
    return out


def without_answer(stored: dict[str, Any], qid: str) -> tuple[dict[str, Any], list[str]]:
    """(answers after undoing ``qid``, ids removed). Undoing a host frees what it absorbed; undoing
    an absorbed question removes it from its host's ``merge_with``."""
    out = dict(stored)
    removed = []
    a = out.pop(qid, None)
    if a is None:
        return out, removed
    removed.append(qid)
    for k, v in list(out.items()):
        if isinstance(v, dict) and v.get("merged_into") == qid:
            del out[k]
            removed.append(k)
    host = a.get("merged_into") if isinstance(a, dict) else None
    if host and isinstance(out.get(host), dict):
        h = dict(out[host])
        ov = dict(h.get("overrides") or {})
        mw = [x for x in ov.get("merge_with") or [] if x != qid]
        if mw:
            ov["merge_with"] = mw
        else:
            ov.pop("merge_with", None)
        h["overrides"] = ov
        out[host] = h
    return out, removed


def check_required(q: dict[str, Any], option_id: str, overrides: dict[str, Any], argv: list[str]) -> None:
    """Options that need an override (``partly --est``) refuse without it."""
    for need in (q.get("requires") or {}).get(option_id, []):
        alts = need.split("|")
        if not any(overrides.get(a) not in (None, "", []) for a in alts):
            flags = " or ".join("--" + a for a in alts)
            raise InputError("input.missing_override", f"{q['id']} {option_id} needs {flags}.",
                             hint=" ".join(argv + ["--" + alts[0], f"<{alts[0]}>"]),
                             details={"question": q["id"], "option": option_id, "requires": alts})


__all__ = ["Answer", "OVERRIDES", "normalize_overrides", "command_args", "bind", "with_answer", "without_answer",
           "resolve_merge_targets", "check_required", "MERGED"]
