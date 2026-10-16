"""Shipped presets under ``docket/presets/``, addressed by id (``markdown/tasks-basic``).

Each preset carries a ``preset:`` metadata block that is never merged into a
workspace:

- ``scope``: ``workspace`` (listed in top-level ``presets``) or ``source``
  (named by ``sources.<id>.extends``; the file holds a ``source:`` template).
- ``scaffold``: the keys ``docket init`` writes so the result validates, every
  value a placeholder for the operator to edit. A source preset's scaffold
  names its source under ``sources`` and any transport it needs.

The description is the first sentence of the file's leading comment.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PRESET_ROOT = Path(__file__).resolve().parent.parent / "presets"
# Sections a preset may never define (TD §3.3): they hold the operator's world.
FORBIDDEN = (("profile", "me"), ("profile", "people"), ("profile", "entities"))
META_KEY = "preset"
SCOPES = ("workspace", "source")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def preset_ids() -> list[str]:
    return sorted(
        p.relative_to(PRESET_ROOT).with_suffix("").as_posix()
        for p in PRESET_ROOT.rglob("*.yaml")
    )


def preset_path(preset_id: str) -> Path | None:
    if ".." in preset_id.split("/"):
        return None
    p = PRESET_ROOT / f"{preset_id}.yaml"
    return p if p.is_file() else None


def description(text: str) -> str:
    """The first sentence of the leading comment block, joined across lines."""
    lines: list[str] = []
    for ln in text.splitlines():
        if not ln.startswith("#"):
            break
        lines.append(ln.lstrip("#").strip())
    para = " ".join(x for x in lines if x)
    return _SENTENCE_END.split(para, maxsplit=1)[0] if para else ""


def scope_of(data: Any) -> str:
    """The declared scope; a preset without metadata is a source preset when it holds ``source:``."""
    meta = data.get(META_KEY) if isinstance(data, dict) else None
    declared = meta.get("scope") if isinstance(meta, dict) else None
    if declared in SCOPES:
        return declared
    return "source" if isinstance(data, dict) and isinstance(data.get("source"), dict) else "workspace"


def scaffold_of(data: Any) -> dict[str, Any]:
    meta = data.get(META_KEY) if isinstance(data, dict) else None
    sc = meta.get("scaffold") if isinstance(meta, dict) else None
    return sc if isinstance(sc, dict) else {}


def usage(scope: str) -> str:
    return ("list it in top-level presets" if scope == "workspace"
            else "name it in sources.<id>.extends (never in top-level presets)")
