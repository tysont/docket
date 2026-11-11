"""Configuration models for the ``journal`` section (TD §13.5).

``journal`` renders the run journal from sections, each a template over the
closed journal context (``docket.config.contexts.JOURNAL``). Without sections
the built-in layout is used. ``destination`` names a ``journal`` destination
the entry is written through; the run directory's ``journal.md`` is always
written. ``ledgers`` append generic rows at the seal, one per element of a
context list, that later journals aggregate across runs. References and
templates are checked in ``docket.seal.checks``.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Context lists a ledger may draw its rows from.
LEDGER_SOURCES: tuple[str, ...] = ("findings", "questions", "answers", "operations", "not_asked", "dropped",
                                   "coverage_notes", "context", "sources", "dispositions", "unexpected")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JournalSection(Strict):
    heading: str
    template: str
    skip_empty: bool = False       # omit the section when its template renders only whitespace


class LedgerSpec(Strict):
    """One row per element of ``from`` that matches every ``where`` pair (string equality)."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)
    from_: Literal["findings", "questions", "answers", "operations", "not_asked", "dropped", "coverage_notes",
                   "context", "sources", "dispositions", "unexpected"] = Field(alias="from")
    key: str
    fields: dict[str, str] = {}
    where: dict[str, str] = {}
    keep_runs: int = Field(30, ge=1, le=1000)   # how many recent runs' rows the journal context carries


class JournalConfig(Strict):
    title: str = "Docket journal: {run.id}"
    sections: list[JournalSection] = []
    destination: str | None = None
    approval_delay_note: str | None = None     # a template over ``approval.*``; absent: no note
    ledgers: dict[str, LedgerSpec] = {}
