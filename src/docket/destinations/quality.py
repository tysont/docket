"""Task quality rules (TD §11.5, FR-ACT-07).

Run on every rendered task line, in order:

1. ``imperative``: a line opening with a denied word or phrase is rewritten
   with ``default_verb`` and recorded as an unexpected shape. Any other opening
   word is accepted: a denylist plus a safe default, never an allowlist.
2. ``require``: each named field must be present. ``link`` is satisfied by any
   link on the line, else filled from the item URL. Other fields are filled from
   the answer (``due``, ``est`` overrides), then ``defaults`` (``due`` may be
   ``+Nd`` from the run date), then the item's ``area``.
3. ``evidence: required``: the item must carry a URL, its text, or a note.
4. ``max_length``: the task text (without fields) must fit.

A line that still fails refuses with ``dest.quality``; ``details.fix`` is the
answer command with the override that fixes it, or the configuration path.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

from docket.cli.errors import IntegrityError
from docket.shapes.unexpected import unexpected
from docket.text.grammar import Grammar, TaskLine

DEFAULT_VERB = "Follow up on"
OVERRIDE_FLAGS = {"due": "--due", "est": "--est", "title": "--title", "note": "--note", "when": "--when"}
PLACEHOLDER = {"due": "<date>", "est": "<duration>", "title": "<shorter imperative title>", "note": "<evidence>",
               "when": "<window>"}
Fix = Callable[[str, str], str]  # (flag, placeholder) -> command text


def _value(name: str, ctx: dict[str, Any], defaults: dict[str, str]) -> str | None:
    v = ctx.get(name)
    if v not in (None, "", []):
        if name == "est":
            from docket.text.durations import format_duration, parse_duration
            try:
                return format_duration(parse_duration(v))
            except Exception:
                return str(v)
        return str(v)
    d = defaults.get(name)
    if d:
        m = re.fullmatch(r"\+(\d+)d", d.strip())
        run_date = ((ctx.get("run") or {}).get("date")) or None
        if m and run_date:
            return (date.fromisoformat(run_date) + timedelta(days=int(m.group(1)))).isoformat()
        if not m:
            return d
    if name == "area" and ctx.get("area"):
        return str(ctx["area"])
    return None


def _refuse(dest_id: str, rule: str, message: str, line: str, field: str | None, fix: Fix | None) -> IntegrityError:
    flag = OVERRIDE_FLAGS.get(field or "")
    if flag and fix is not None:
        fix_text = fix(flag, PLACEHOLDER[field or ""])
    else:
        fix_text = f"docket config explain destinations.{dest_id}.quality (set quality.defaults.{field or rule})"
    return IntegrityError("dest.quality", f"{dest_id}: {message}", hint=fix_text,
                          details={"destination": dest_id, "rule": rule, "field": field, "line": line, "fix": fix_text})


def check_line(quality: Any, grammar: Grammar, line: str, ctx: dict[str, Any], *, dest_id: str,
               fix: Fix | None = None) -> str:
    """Return the line after rewrites and fills, or raise ``dest.quality``."""
    if quality is None:
        return line
    q = quality if isinstance(quality, dict) else quality.model_dump()
    t: TaskLine | None = grammar.parse(line)
    if t is None:
        unexpected("dest.grammar", "rendered task line does not parse", sample=line,
                   reading="write the line as rendered; skip field quality rules",
                   extend=f"destinations.{dest_id}.grammar or destinations.{dest_id}.line_template")
        text_len = len(line)
    else:
        imp = q.get("imperative") or None
        if imp and imp.get("deny"):
            t = _imperative(t, imp, dest_id)
        defaults = q.get("defaults") or {}
        for name in q.get("require") or []:
            if name == "link":
                if t.links:
                    continue
                if ctx.get("url"):
                    t = t.with_field("link", str(ctx["url"]))
                    continue
                raise _refuse(dest_id, "require", "the task has no link and the item has no URL",
                              grammar.render(t), "link", fix)
            if t.get(name) not in (None, ""):
                continue
            val = _value(name, ctx, defaults)
            if val is None:
                raise _refuse(dest_id, "require", f"required field {name!r} is missing", grammar.render(t), name, fix)
            t = t.with_field(name, val)
        text_len = len(t.text)
        line = grammar.render(t)
    if q.get("evidence") == "required" and not any(ctx.get(k) for k in ("url", "text", "note")):
        raise _refuse(dest_id, "evidence", "the task carries no evidence (no URL, text or note)", line, "note", fix)
    mx = q.get("max_length")
    if mx and text_len > int(mx):
        raise _refuse(dest_id, "max_length", f"the task text is {text_len} characters; the limit is {mx}",
                      line, "title", fix)
    return line


def _imperative(t: TaskLine, imp: dict[str, Any], dest_id: str) -> TaskLine:
    words = t.text.split()
    low = [re.sub(r"[^\w'-]+$", "", w).lower() for w in words]
    for phrase in sorted(imp["deny"], key=lambda p: -len(p.split())):
        pw = phrase.lower().split()
        if pw and low[: len(pw)] == pw:
            rest = " ".join(words[len(pw):]).lstrip(" :,;-\u2013\u2014")
            verb = imp.get("default_verb") or DEFAULT_VERB
            new = f"{verb} {rest}".strip() if rest else verb
            unexpected("dest.quality", "task opens with a denied word", sample=phrase,
                       reading=f"rewrite the opening as {verb!r}",
                       extend=f"destinations.{dest_id}.quality.imperative")
            return t.with_text(new)
    return t
