"""Redaction for shared destinations, and leak validation (TD §11.4, NFR-02).

``Redactor.redact`` rewrites protected entity aliases, people's names and
aliases (when ``people`` is on) and configured figure patterns. Matching is
case-insensitive on whole words, with any whitespace between an alias's words.

``Redactor.leaks`` is deliberately broader than ``redact``: it tokenizes the
text on every non-alphanumeric character and reports an alias whose tokens
appear in sequence, or concatenated into one token (``ExampleCo`` for
``Example Co``). A form redaction did not rewrite is therefore refused rather
than published. ``check`` raises ``dest.redaction_leak`` naming the field and
the kind of term, never the term itself.

Items with a sensitivity class (``fields.<class_field>``) render their title
through the class's template instead of their own text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from docket.cli.errors import IntegrityError
from docket.text import templates

DEFAULTS = {"entity": "[redacted]", "person": "[person]", "figure": "[figure]", "class_field": "sensitivity"}
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019&.-][A-Za-z0-9]+)*")
_LINK = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def _alias_pattern(alias: str) -> re.Pattern[str]:
    words = alias.split()
    body = r"\s+".join(re.escape(w) for w in words)
    left = r"(?<![A-Za-z0-9])" if alias[:1].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if alias[-1:].isalnum() else ""
    return re.compile(left + body + right, re.I)


@dataclass
class Term:
    kind: str  # entity | person
    ident: str
    alias: str
    tokens: tuple[str, ...]


@dataclass
class Leak:
    kind: str
    ident: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.ident}


@dataclass
class Redactor:
    terms: list[Term] = field(default_factory=list)
    figures: list[re.Pattern[str]] = field(default_factory=list)
    classes: dict[str, str] = field(default_factory=dict)
    entity: str = DEFAULTS["entity"]
    person: str = DEFAULTS["person"]
    figure: str = DEFAULTS["figure"]
    class_field: str = DEFAULTS["class_field"]
    allow: list[re.Pattern[str]] | None = None      # None: names are not redacted
    private: list[re.Pattern[str]] = field(default_factory=list)
    private_title: str = "Private"

    @classmethod
    def from_config(cls, profile: Any, cfg: Any = None) -> "Redactor":
        """Build from the workspace profile and a destination's ``redact`` block (dict or model)."""
        c: dict[str, Any] = {}
        if cfg is not None:
            c = cfg if isinstance(cfg, dict) else cfg.model_dump()
        terms: list[Term] = []
        for e in getattr(profile, "entities", []) or []:
            if e.protected:
                for a in [*e.aliases]:
                    if a.strip():
                        terms.append(Term("entity", e.id, a, tuple(_tokens(a))))
        if c.get("people"):
            for p in getattr(profile, "people", []) or []:
                for a in [p.name, *p.aliases]:
                    if a and a.strip():
                        terms.append(Term("person", p.id, a, tuple(_tokens(a))))
        # longest aliases first, so "Example Co East" wins over "Example Co"
        terms.sort(key=lambda t: (-len(t.alias), t.alias))
        return cls(
            terms=[t for t in terms if t.tokens],
            figures=[re.compile(p) for p in (c.get("figures") or [])],
            classes=dict(c.get("classes") or {}),
            entity=c.get("entity") or DEFAULTS["entity"],
            person=c.get("person") or DEFAULTS["person"],
            figure=c.get("figure") or DEFAULTS["figure"],
            class_field=c.get("class_field") or DEFAULTS["class_field"],
            allow=None if not c.get("names") else [
                _alias_pattern(a) for a in sorted((c["names"].get("allow") or []), key=lambda a: (-len(a), a))
                if a.strip()],
            private=[re.compile(p) for p in ((c.get("private") or {}).get("patterns") or [])],
            private_title=(c.get("private") or {}).get("title") or "Private",
        )

    # --- rewriting ---------------------------------------------------------
    def redact(self, text: str) -> str:
        if not text:
            return text
        out = text
        for t in self.terms:
            out = _alias_pattern(t.alias).sub(self.entity if t.kind == "entity" else self.person, out)
        for rx in self.figures:
            out = rx.sub(self.figure, out)
        if self.allow is not None:
            out = self._names(out)
        return out

    def _names(self, text: str) -> str:
        """Replace every word written like a name (it has a capital letter) that the workspace
        does not allow. Links, allowed words and phrases, and the placeholders stay."""
        keep: list[tuple[int, int]] = [m.span() for m in _LINK.finditer(text)]
        for ph in {self.entity, self.person, self.figure, self.private_title}:
            keep += [m.span() for m in re.finditer(re.escape(ph), text)]
        for rx in self.allow or []:
            keep += [m.span() for m in rx.finditer(text)]

        def kept(a: int, b: int) -> bool:
            return any(s <= a and b <= e for s, e in keep)

        out, pos, last_end = [], 0, None
        for m in _WORD.finditer(text):
            if kept(*m.span()) or not any(ch.isupper() for ch in m.group(0)):
                continue
            end = m.end()
            poss = re.match(r"['\u2019]s\b", text[end:])      # a possessive goes with its name
            if poss:
                end += poss.end()
            if last_end is not None and text[last_end:m.start()].isspace() and out and out[-1] == self.entity:
                pos = end                                       # two names in a row are one name
            else:
                out.append(text[pos:m.start()])
                out.append(self.entity)
                pos = end
            last_end = end
        out.append(text[pos:])
        return "".join(out)

    def is_private(self, *texts: str | None) -> bool:
        return any(rx.search(t) for t in texts if t for rx in self.private)

    def title(self, text: str, *, source: str | None = None) -> str:
        """Text for a shared destination: the private title when it, or what it was made
        from, is a private matter; otherwise the text redacted."""
        return self.private_title if self.is_private(text, source) else self.redact(text)

    def sensitivity(self, ctx: dict[str, Any]) -> str | None:
        cls_name = (ctx.get("fields") or {}).get(self.class_field) or ctx.get(self.class_field)
        return cls_name if isinstance(cls_name, str) and cls_name in self.classes else None

    def render_title(self, template: str, ctx: dict[str, Any]) -> str:
        """Render a title for a shared destination: the class template when the item has one, then redact."""
        cls_name = self.sensitivity(ctx)
        tpl = self.classes[cls_name] if cls_name else template
        return self.redact(templates.render(tpl, ctx, redactor=self.redact))

    def render_text(self, template: str, ctx: dict[str, Any]) -> str:
        """Render notes or other free text: empty for a sensitive item, otherwise redacted."""
        if self.sensitivity(ctx):
            return ""
        return self.redact(templates.render(template, ctx, redactor=self.redact))

    # --- validation --------------------------------------------------------
    def leaks(self, text: str) -> list[Leak]:
        toks = _tokens(text or "")
        if not toks:
            return []
        found: dict[tuple[str, str], Leak] = {}
        joined = set(toks)
        for t in self.terms:
            n = len(t.tokens)
            hit = any(tuple(toks[i:i + n]) == t.tokens for i in range(len(toks) - n + 1))
            if not hit and n > 1:
                hit = "".join(t.tokens) in joined
            if hit:
                found.setdefault((t.kind, t.ident), Leak(t.kind, t.ident))
        return list(found.values())

    def check(self, text: str, *, where: str, hint: str | None = None) -> None:
        hits = self.leaks(text)
        if hits:
            kinds = sorted({h.kind for h in hits})
            raise IntegrityError(
                "dest.redaction_leak",
                f"{where}: rendered text for a shared destination names a protected {' and '.join(kinds)}.",
                hint=hint or "Add the form to the profile aliases, or set a sensitivity class template",
                details={"field": where, "leaks": [h.as_dict() for h in hits]},
            )

    def check_all(self, texts: Iterable[tuple[str, str]], hint: str | None = None) -> None:
        for where, text in texts:
            self.check(text, where=where, hint=hint)
