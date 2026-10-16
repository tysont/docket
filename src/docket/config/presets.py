"""Shipped presets under ``docket/presets/``, addressed by id (``markdown/tasks-basic``)."""
from __future__ import annotations

from pathlib import Path

PRESET_ROOT = Path(__file__).resolve().parent.parent / "presets"
# Sections a preset may never define (TD §3.3): they hold the operator's world.
FORBIDDEN = (("profile", "me"), ("profile", "people"), ("profile", "entities"))


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
