"""Configuration models for the ``tasks`` section (TD §9).

``tasks`` sets the dispatch concurrency, each core kind's pack budget and
vocabulary, and declares workspace-defined kinds. A workspace kind names the
delegated stage that issues it, which items it reads (``select``), its output
fields from a closed field-type set, cross-field rules and its instructions.
Docket renders and validates a workspace kind exactly like a core kind
(FR-TSK-01); see ``docket.tasks.kinds``. References and templates are checked
in ``docket.config.task_checks``.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The closed field-type set a workspace kind may use (TD §9.1).
FIELD_TYPES: tuple[str, ...] = ("str", "int", "bool", "enum", "date", "duration", "evidence", "item_ref")
FieldTypeName = Literal["str", "int", "bool", "enum", "date", "duration", "evidence", "item_ref"]
RESERVED_FIELDS = frozenset({"id", "source"})
CORE_KIND_NAMES: tuple[str, ...] = ("assess", "extract", "review", "classify")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskBudget(Strict):
    """Pack budget (TD §9.2): at most ``max_items`` items and ``max_bytes`` bytes of item content per pack."""
    max_items: int = Field(10, ge=1, le=500)
    max_bytes: int = Field(60000, ge=512)


class AskVocabulary(Strict):
    """The ``ask`` verb vocabulary: a first word in ``deny`` takes ``default_verb`` instead (TD §9.4)."""
    deny: list[str] = []
    default_verb: str | None = None


class AssessTasks(TaskBudget):
    kinds: dict[str, str] = {}          # extra item-kind values -> meaning, added to the core set
    deny_kinds: list[str] = []          # values read as the safe kind (fyi), added to the core denylist
    ask: AskVocabulary = AskVocabulary()
    max_evidence: int = Field(3, ge=1, le=20)
    max_quote: int = Field(300, ge=20, le=4000)
    guidance: str | None = None         # operator guidance appended to the core instructions


class ReviewTasks(TaskBudget):
    guidance: str | None = None


class TaskSelect(Strict):
    """Which items a kind reads. Unset keys do not filter."""
    sources: list[str] | None = None
    units: list[str] | None = None
    kept: bool | None = None            # review-stage kinds: only items the assessment kept


class ExtractTasks(TaskBudget):
    """``extract`` runs only when ``select`` names sources (documents such as transcripts)."""
    select: TaskSelect | None = None
    guidance: str | None = None
    max_quote: int = Field(300, ge=20, le=4000)


class ClassifyTasks(TaskBudget):
    """``classify`` runs only when ``select`` is set; ``classes`` replaces the core class set."""
    select: TaskSelect | None = None
    classes: dict[str, str] = {}
    safe: str | None = None
    guidance: str | None = None

    @model_validator(mode="after")
    def _safe_in_classes(self) -> "ClassifyTasks":
        if self.safe is not None and self.classes and self.safe not in self.classes:
            raise ValueError(f"safe {self.safe!r} is not one of classes {sorted(self.classes)}")
        return self


class TaskFieldConfig(Strict):
    """One output field of a workspace kind, from the closed field-type set."""
    type: FieldTypeName
    description: str
    required: bool = False
    values: dict[str, str] = {}         # enum: value -> meaning
    safe: str | None = None             # enum: the reading of an unknown or denied value (open vocabulary)
    deny: list[str] = []                # enum: values always read as ``safe``
    max_length: int | None = Field(None, ge=1)
    max_items: int = Field(3, ge=1, le=20)  # evidence: most quotes per row

    @model_validator(mode="after")
    def _enum(self) -> "TaskFieldConfig":
        if self.type == "enum":
            if not self.values:
                raise ValueError("an enum field needs values (value: meaning)")
            if self.safe is not None and self.safe not in self.values:
                raise ValueError(f"safe {self.safe!r} is not one of values {sorted(self.values)}")
        elif self.values or self.safe or self.deny:
            raise ValueError("values, safe and deny apply to enum fields only")
        return self


class TaskRuleConfig(Strict):
    """A cross-field rule: when every ``when`` field equals its value, the ``require`` fields must be set."""
    when: dict[str, Any]
    require: list[str] = Field(min_length=1)
    reason: str | None = None


class TaskKindConfig(TaskBudget):
    """A workspace-defined task kind (TD §9.1)."""
    stage: Literal["assess", "review"] = "assess"
    description: str | None = None
    instructions: str
    select: TaskSelect = TaskSelect()
    fields: dict[str, TaskFieldConfig] = Field(min_length=1)
    rules: list[TaskRuleConfig] = []

    @model_validator(mode="after")
    def _names(self) -> "TaskKindConfig":
        bad = sorted(set(self.fields) & RESERVED_FIELDS)
        if bad:
            raise ValueError(f"field names {bad} are reserved (every row carries the pack item id)")
        for r in self.rules:
            unknown = sorted((set(r.when) | set(r.require)) - set(self.fields))
            if unknown:
                raise ValueError(f"rule names unknown fields {unknown}")
        return self


class TasksConfig(Strict):
    concurrency: int = Field(6, ge=1, le=64)
    assess: AssessTasks = AssessTasks()
    review: ReviewTasks = ReviewTasks()
    extract: ExtractTasks = ExtractTasks()
    classify: ClassifyTasks = ClassifyTasks()
    kinds: dict[str, TaskKindConfig] = {}

    @field_validator("kinds")
    @classmethod
    def _kind_names(cls, v: dict[str, TaskKindConfig]) -> dict[str, TaskKindConfig]:
        bad = sorted(k for k in v if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", k))
        if bad:
            raise ValueError(f"tasks.kinds names {bad} must be lower-case words ([a-z][a-z0-9_-]*)")
        clash = sorted(set(v) & set(CORE_KIND_NAMES))
        if clash:
            raise ValueError(f"tasks.kinds {clash} reuse a core kind name")
        return v
