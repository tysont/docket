"""Profile read-side helpers for items (TD §3.2, §7.1, FR-SIG-03).

``ProfileIndex`` resolves a sender handle to a profile person and the
relationship sets that person belongs to, and finds mentions of the operator,
people, groups, topics and entities in text. Every word it matches comes from
the profile; nothing here names a person or a vocabulary.

Relationship precedence for ``relation`` (the primary set): ``me``, ``chain``,
``reports``, ``skip_reports``, ``peers``, ``external``, then the first group or
role set, then ``known`` (a profile person in no set) or ``unknown``.
``relations`` lists every set the sender is in, so a rule on ``group:<id>``
matches a report who is also a group member. An unknown handle is valid: its
relation is ``unknown`` (or ``external`` when its mail domain is not internal).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from docket.config.models import Profile, relationship_sets

PRECEDENCE = ("chain", "reports", "skip_reports", "peers", "external")


def _domain(handle: str | None) -> str | None:
    if handle and "@" in handle:
        return handle.rsplit("@", 1)[1].strip().lower().rstrip(">") or None
    return None


@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern[str]:
    """Word-bounded, case-insensitive; a short all-caps alias (``AB``) matches case-sensitively."""
    left = r"(?<![A-Za-z0-9])" if term[:1].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if term[-1:].isalnum() else ""
    flags = 0 if (len(term) <= 3 and term.isupper()) else re.I
    return re.compile(left + re.escape(term) + right, flags)


def _any(terms: list[str], text: str) -> bool:
    return any(t and term_pattern(t).search(text) for t in terms)


class ProfileIndex:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.sets = relationship_sets(profile)
        self.internal = {d.lower() for d in profile.internal_domains}
        me = profile.me
        self.me_handles = {h.strip().lower() for h in me.handles.values() if h}
        self.me_terms = [t for t in [me.name, *me.aliases] if t]
        self.handles: dict[str, str] = {}
        self.names: dict[str, str] = {}
        for p in profile.people:
            for h in p.handles.values():
                if h:
                    self.handles.setdefault(h.strip().lower(), p.id)
            for n in [p.name, *p.aliases]:
                if n:
                    self.names.setdefault(n.strip().lower(), p.id)
        self.person_sets: dict[str, list[str]] = {}
        for name, members in self.sets.items():
            for pid in members:
                self.person_sets.setdefault(pid, []).append(name)
        self.people_terms = {p.id: [t for t in [p.name, *p.aliases] if t] for p in profile.people}
        self.group_terms = {g.id: [g.name] if g.name else [] for g in profile.groups}
        self.topics = {t.id: t for t in profile.topics}
        self.topic_terms = {t.id: [t.id, *t.aliases] for t in profile.topics}
        self.entities = {e.id: e for e in profile.entities}
        self.entity_terms = {e.id: [e.id, *e.aliases] for e in profile.entities}

    # --- senders ---------------------------------------------------------------
    def person_for(self, handle: str | None, display: str | None = None) -> str | None:
        if handle and handle.strip().lower() in self.handles:
            return self.handles[handle.strip().lower()]
        for v in (display, handle):
            if v and v.strip().lower() in self.names:
                return self.names[v.strip().lower()]
        return None

    def is_me(self, handle: str | None, display: str | None = None) -> bool:
        if handle and handle.strip().lower() in self.me_handles:
            return True
        name = self.profile.me.name.strip().lower()
        return any(v and v.strip().lower() == name for v in (display, handle))

    def sender(self, handle: str | None, display: str | None = None) -> dict[str, Any]:
        handle = (handle or "").strip() or None
        display = (display or "").strip() or None
        dom = _domain(handle)
        out: dict[str, Any] = {"handle": handle, "display": display, "person": None, "relation": "unknown",
                               "relations": [], "is_me": False, "domain": dom}
        if self.is_me(handle, display):
            out.update(is_me=True, relation="me", relations=["me"])
            return out
        pid = self.person_for(handle, display)
        rels = list(self.person_sets.get(pid, [])) if pid else []
        if dom and self.internal and dom not in self.internal and "external" not in rels:
            rels.append("external")
        primary = next((r for r in PRECEDENCE if r in rels), None) or (rels[0] if rels else None)
        if pid:
            rels.append("known")
        else:
            rels.append("unknown")
        out.update(person=pid, relations=rels, relation=primary or ("known" if pid else "unknown"))
        return out

    # --- mentions --------------------------------------------------------------
    def mentions(self, text: str, handles: list[str] | None = None) -> dict[str, Any]:
        hs = {h.strip().lower() for h in (handles or []) if h}
        me = bool(hs & self.me_handles) or _any(self.me_terms, text) or \
            any(h and h in text.lower() for h in self.me_handles if len(h) > 3)
        people = sorted({pid for pid, terms in self.people_terms.items() if _any(terms, text)} |
                        {self.handles[h] for h in hs if h in self.handles})
        groups = sorted(g for g, terms in self.group_terms.items() if terms and _any(terms, text))
        topics = sorted(t for t, terms in self.topic_terms.items() if _any(terms, text))
        entities = sorted(e for e, terms in self.entity_terms.items() if _any(terms, text))
        return {"me": me, "people": people, "groups": groups, "topics": topics, "entities": entities}
