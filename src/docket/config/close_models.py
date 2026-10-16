"""Configuration models for the ``journal`` and ``hooks`` sections (TD §13.5, §13.6).

``journal`` renders the run journal from sections, each a template over the
closed journal context (``docket.config.contexts.JOURNAL``). Without sections
the built-in layout is used. ``destination`` names a ``journal`` destination
the entry is written through; the run directory's ``journal.md`` is always
written. ``ledgers`` append generic rows at close, one per element of a
context list, that later journals aggregate across runs.

``hooks`` declares commands run at ``after_collect``, ``after_apply`` and
``after_close``. A workflow's ``hooks`` map (event -> hook ids) replaces the
default selection for that event. References and templates are checked in
``docket.close.checks``.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HOOK_EVENTS: tuple[str, ...] = ("after_collect", "after_apply", "after_close")
HookEvent = Literal["after_collect", "after_apply", "after_close"]
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


class Hook(Strict):
    description: str | None = None
    command: list[str] = Field(min_length=1)
    events: list[HookEvent] = Field(min_length=1)
    cwd: str | None = None                      # relative to the workspace root
    timeout_s: float = Field(60.0, gt=0, le=3600)
    on_failure: Literal["warn", "fail"] = "warn"
    env: list[str] = []                         # environment variables passed through by name
    dry_run: list[str] | None = None            # the argv of a side-effect-free form, for --dry-run and sandbox runs
    workflows: list[str] | None = None          # default: every workflow

    @field_validator("env")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        import re
        for n in v:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n):
                raise ValueError(f"not an environment variable name: {n!r}")
        return v
