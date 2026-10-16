"""Helpers for agent-task tests: persona configurations, invented items and packs without a run."""
from __future__ import annotations

from typing import Any

from docket.config import load_workspace
from docket.tasks.evidence import normalize
from docket.tasks.kinds import TaskKind, kinds_for
from docket.tasks.packs import PackContext, build_packs
from tests.conftest import PERSONAS

PERSONA_NAMES = ("reference-like", "support-lead")


def persona_config(name: str) -> Any:
    return load_workspace(PERSONAS / name).config


def persona_kinds(name: str) -> dict[str, TaskKind]:
    return kinds_for(persona_config(name))


def item(n: int, *, source: str = "chat", text: str | None = None, score: int | None = None,
         **extra: Any) -> dict[str, Any]:
    it: dict[str, Any] = {
        "id": f"item{n:03d}", "source": source, "unit": "room-a", "title": f"Invented item {n}",
        "text": text if text is not None else f"Please send the invented report {n} by Friday.\nThanks.",
        "from": "Writer", "at": "2026-10-15T09:00:00Z", "url": "", "fields": {}, "record_id": f"r_{n:03d}",
    }
    if score is not None:
        it["score"] = score
    it.update(extra)
    return it


def packs_for(kind: TaskKind, items: list[dict[str, Any]], config: Any) -> list[dict[str, Any]]:
    return build_packs(kind, items, PackContext.build(config))


def raw_from_lines(pack: dict[str, Any]):
    """A raw-text provider for ``submit.validate``: each item's lines stand in for its raw record."""
    texts = {it["id"]: normalize("\n".join(ln["text"] for ln in it["lines"])) for it in pack["items"]}
    return lambda iid: texts.get(iid)
