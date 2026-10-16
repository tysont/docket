"""Litmus test 4.1: nothing hardcoded (Test Plan §4.1, PRD §12).

Forbidden literals come from:

1. A private denylist kept outside the repository: ``$DOCKET_LITMUS_DENYLIST``,
   else ``~/.config/docket/denylist.txt`` of the real home directory. The path
   is computed when this module is imported, before the isolation fixture fakes
   HOME. The file is only read. When it is absent the source is skipped.
2. Identifiers of both persona fixture workspaces: names, aliases, handles,
   mail domains, paths and title-pattern literals.

``src/docket`` (presets included) is scanned case-insensitively on word
boundaries. Hits are reported with file, line, the source list and the term
masked, so a failure never republishes a private term.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

pytestmark = pytest.mark.smoke

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "docket"
PERSONAS = REPO / "tests" / "fixtures" / "personas"

# Captured at import, before any fixture replaces HOME. Reading is allowed; nothing is written.
_DENYLIST_PATH = os.environ.get("DOCKET_LITMUS_DENYLIST") or os.path.join(
    os.path.expanduser("~"), ".config", "docket", "denylist.txt")

# Generic words that collide with persona identifiers. Each entry names its generic meaning.
ALLOWLIST = {
    "me",  # the operator's self-reference in reports_to, and English
}


def mask(term: str) -> str:
    return term[:2] + "*" * max(0, len(term) - 2) if len(term) > 2 else "**"


def _pattern(term: str) -> re.Pattern[str]:
    left = r"(?<![A-Za-z0-9])" if term[0].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
    return re.compile(left + re.escape(term) + right, re.I)


def denylist_terms() -> list[str]:
    p = Path(_DENYLIST_PATH)
    if not p.is_file():
        return []
    out = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        t = raw.strip()
        if t and not t.startswith("#"):
            out.append(t)
    return out


def _regex_literal(rx: str) -> str:
    s = re.sub(r"\\[bBAzZ]", " ", rx)
    s = re.sub(r"[\^\$\(\)\[\]\{\}\|\?\*\+\\]", " ", s)
    return " ".join(s.split())


def persona_terms() -> list[str]:
    yaml = YAML(typ="safe")
    terms: set[str] = set()

    def add(v: object) -> None:
        if isinstance(v, str) and v.strip():
            terms.add(v.strip())
            if "@" in v:
                terms.add(v.rsplit("@", 1)[1])

    for ws in sorted(p for p in PERSONAS.iterdir() if p.is_dir()):
        for f in sorted(ws.rglob("*.yaml")):
            data = yaml.load(f.read_text(encoding="utf-8")) or {}
            prof = data.get("profile") or {}
            me = prof.get("me") or {}
            add(me.get("name"))
            for a in me.get("aliases") or []:
                add(a)
            for h in (me.get("handles") or {}).values():
                add(h)
            for d in prof.get("internal_domains") or []:
                add(d)
            for sect in ("people", "groups", "topics", "entities", "forums"):
                for row in prof.get(sect) or []:
                    add(row.get("name"))
                    for a in row.get("aliases") or []:
                        add(a)
                    for h in (row.get("handles") or {}).values():
                        add(h)
                    rx = (row.get("calendar_match") or {}).get("title_regex")
                    if rx:
                        add(_regex_literal(rx))
            for rule in (prof.get("calendar") or {}).get("classify") or []:
                rx = (rule.get("match") or {}).get("title_regex")
                if rx:
                    add(_regex_literal(rx))
            for sect in ("sources", "destinations"):
                for inst in (data.get(sect) or {}).values():
                    if isinstance(inst, dict) and inst.get("path"):
                        add(inst["path"])
    return sorted(t for t in terms if t.lower() not in ALLOWLIST)


def scan(terms: list[str], label: str) -> list[str]:
    pats = [(_pattern(t), t) for t in terms]
    hits = []
    for f in sorted(SRC.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = f.relative_to(REPO).as_posix()
        for n, line in enumerate(text.splitlines(), 1):
            for pat, term in pats:
                if pat.search(line):
                    hits.append(f"{rel}:{n}: {label} literal '{mask(term)}'")
    return hits


def test_persona_terms_are_collected():
    terms = persona_terms()
    assert len(terms) > 30  # names, aliases, handles, domains, paths, title literals of both personas


def test_no_persona_literals_in_src():
    hits = scan(persona_terms(), "persona")
    if hits:
        pytest.fail("persona literals found in src:\n" + "\n".join(hits), pytrace=False)


def test_no_denylist_literals_in_src():
    terms = denylist_terms()
    if not terms:
        pytest.skip("no private denylist on this machine")
    hits = scan(terms, "denylist")
    if hits:
        pytest.fail("denylist literals found in src:\n" + "\n".join(hits), pytrace=False)


def test_scanner_finds_a_seeded_literal(tmp_path):
    """The scanner itself works: a seeded term in a seeded line is found and masked."""
    pat = _pattern("Northwind Widget")
    assert pat.search("owner: northwind widget team")
    assert not pat.search("northwind widgets")
    assert mask("Northwind") == "No*******"
