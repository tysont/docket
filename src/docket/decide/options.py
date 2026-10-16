"""Forgiving option resolution (TD §10.3, AE-09).

An answer may name an option by id, label, the label's leading words, its
1-based position, or its destination, case-insensitively. Exactly one match
wins; several raise ``input.ambiguous_option`` with the candidates; none
raises ``input.unknown_option``.
"""
from __future__ import annotations

import re
from typing import Any

from docket.cli.errors import InputError


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(s).lower()).split())


def resolve_option(text: str, options: list[dict[str, Any]], *, question_id: str = "") -> dict[str, Any]:
    raw = str(text).strip()
    q = _norm(raw)
    cands = [{"id": o["id"], "label": o.get("label", "")} for o in options]
    if not q:
        raise InputError("input.unknown_option", "Empty answer.", details={"options": cands})
    if raw.isdigit():
        pos = int(raw)
        if 1 <= pos <= len(options):
            return options[pos - 1]
    tiers = [
        [o for o in options if o["id"].lower() == raw.lower()],
        [o for o in options if _norm(o.get("label", "")) == q],
        [o for o in options if o.get("destination") and str(o["destination"]).lower() == raw.lower()],
        [o for o in options if _norm(o.get("label", "")).startswith(q + " ") or _norm(o["id"]).startswith(q)],
        [o for o in options if _norm(o.get("label", "")).startswith(q)],
    ]
    for matches in tiers:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InputError(
                "input.ambiguous_option",
                f"{raw!r} matches {len(matches)} options: " + ", ".join(m["id"] for m in matches),
                hint=f"docket ask show {question_id}".strip(),
                details={"candidates": [{"id": m["id"], "label": m.get("label", "")} for m in matches]},
            )
    raise InputError(
        "input.unknown_option", f"{raw!r} names no option" + (f" of {question_id}" if question_id else ""),
        hint=f"docket ask show {question_id}".strip(), details={"options": cands},
    )
