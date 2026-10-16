"""Configuration models for ``signals`` (TD §8): rules, references, scoring and budgets.

``signals`` stays an open map on ``DocketConfig`` (``signals.units`` is modelled in
``docket.config.source_models``); this module parses the rest strictly.
``docket.signals.checks`` reports its errors with file, line and the closest valid
key; ``signals_config(ws)`` returns the parsed model of a loaded workspace.

The rule language is closed. A ``when`` node holds any of the predicates below
and the combinators ``all``, ``any`` and ``not``; every key of one node must hold.
Effects are the fields of ``Then``. Shorthands: ``source: chat``, ``to: direct``,
``reference: incident``, ``plugin: name``, ``unanswered: 2h``, ``text: <regex>``,
``mentions.topics: [id]``, ``expect_in: <source>``.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

POSITIONS = ("direct", "cc", "mentioned", "member")
COMPONENTS = ("importance", "urgency", "agent")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _listify(v: Any) -> Any:
    if v is None or isinstance(v, list):
        return v
    return [v]


def _short(key: str, *, as_list: bool = True) -> Any:
    """A before-validator turning a scalar or list into ``{key: value}``."""
    def conv(cls: Any, v: Any) -> Any:
        if isinstance(v, dict) or v is None:
            return v
        return {key: _listify(v) if as_list else v}
    return model_validator(mode="before")(classmethod(conv))


def _lists(*names: str) -> Any:
    def conv(cls: Any, v: Any) -> Any:
        return _listify(v)
    return field_validator(*names, mode="before")(classmethod(conv))


# --- predicates (TD §8.1) ----------------------------------------------------


class SourcePred(Strict):
    """Source id or kind (the transport kind, or ``files``)."""
    id: list[str] | None = None
    kind: list[str] | None = None
    _s = _short("id")
    _l = _lists("id", "kind")


class UnitPred(Strict):
    """Unit attributes from the unit registry (TD §6.9)."""
    id: list[str] | None = None
    id_regex: str | None = None
    name_regex: str | None = None
    kind: list[str] | None = None
    class_: list[str] | None = Field(None, alias="class")
    tier: list[int | str] | None = None
    tier_max: int | None = None
    _l = _lists("id", "kind", "class_", "tier")


class FromPred(Strict):
    """The sender, resolved through profile handles. ``relation`` names a relationship set
    (``chain``, ``reports``, ``skip_reports``, ``peers``, ``external``, ``group:<id>``,
    ``role:<name>``) or ``me``, ``known``, ``unknown``."""
    relation: list[str] | None = None
    person: list[str] | None = None
    group: list[str] | None = None
    domain: list[str] | None = None
    handle: list[str] | None = None        # regexes over the raw sender handle
    is_me: bool | None = None
    _l = _lists("relation", "person", "group", "domain", "handle")


class ToPred(Strict):
    """The operator's position: ``direct`` (addressed or a direct unit), ``cc``, ``mentioned``, ``member``."""
    position: list[Literal["direct", "cc", "mentioned", "member"]]
    _s = _short("position")
    _l = _lists("position")


class PeopleSel(Strict):
    id: list[str] | None = None
    relation: list[str] | None = None
    _s = _short("id")
    _l = _lists("id", "relation")


class TopicSel(Strict):
    id: list[str] | None = None
    owned: bool | None = None
    area: list[str] | None = None
    _s = _short("id")
    _l = _lists("id", "area")


class EntitySel(Strict):
    id: list[str] | None = None
    protected: bool | None = None
    strategic: bool | None = None
    _s = _short("id")
    _l = _lists("id")


class MentionsPred(Strict):
    me: bool | None = None
    people: PeopleSel | None = None
    groups: list[str] | None = None
    topics: TopicSel | None = None
    entities: EntitySel | None = None
    _l = _lists("groups")


class TextPred(Strict):
    """Regexes or phrases over the item's title and text."""
    regex: list[str] | None = None
    phrases: list[str] | None = None
    case: Literal["insensitive", "sensitive"] = "insensitive"
    fields: list[Literal["title", "text"]] = ["title", "text"]
    _s = _short("regex")
    _l = _lists("regex", "phrases", "fields")


class FieldPred(Strict):
    """A mapped field (``fields.<name>``): equals, in, regex, range, present."""
    name: str
    equals: str | int | float | bool | None = None
    in_: list[str | int | float | bool] | None = Field(None, alias="in")
    regex: str | None = None
    min: float | None = None
    max: float | None = None
    present: bool | None = None


class AgePred(Strict):
    older_than: str | int | None = None
    newer_than: str | int | None = None


class UnansweredPred(Strict):
    """No reply from the operator in the item's thread, and the item is at least this old."""
    for_: str | int = Field("0m", alias="for")
    _s = _short("for", as_list=False)


class ThreadPred(Strict):
    started_by_me: bool | None = None
    i_replied: bool | None = None
    replies_min: int | None = None
    replies_max: int | None = None


class ReferencePred(Strict):
    """The item carries a reference of a named ``signals.references`` pattern."""
    pattern: list[str]
    _s = _short("pattern")
    _l = _lists("pattern")


class PluginPred(Strict):
    """A named predicate registered by a plugin (``docket.plugins`` entry points)."""
    name: str
    args: dict[str, Any] = {}
    _s = _short("name", as_list=False)


PREDICATES = ("source", "unit", "from", "to", "mentions", "text", "field", "age", "unanswered", "thread",
              "reference", "plugin")
COMBINATORS = ("all", "any", "not")


class When(Strict):
    """A condition node. Every key present must hold."""
    all_: list["When"] | None = Field(None, alias="all")
    any_: list["When"] | None = Field(None, alias="any")
    not_: "When | None" = Field(None, alias="not")
    source: SourcePred | None = None
    unit: UnitPred | None = None
    from_: FromPred | None = Field(None, alias="from")
    to: ToPred | None = None
    mentions: MentionsPred | None = None
    text: TextPred | None = None
    field_: list[FieldPred] | None = Field(None, alias="field")
    age: AgePred | None = None
    unanswered: UnansweredPred | None = None
    thread: ThreadPred | None = None
    reference: ReferencePred | None = None
    plugin: PluginPred | None = None
    _l = _lists("field_")

    @model_validator(mode="after")
    def _non_empty(self) -> "When":
        if not self.model_fields_set:
            raise ValueError("a condition needs at least one predicate or combinator")  # raise-ok: pydantic validator; config loading reports it as config.invalid
        return self


# --- effects -------------------------------------------------------------------


class DeadlineEff(Strict):
    """Extract a deadline from a mapped field or a regex over title and text (group ``date`` or 1)."""
    field: str | None = None
    pattern: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "DeadlineEff":
        if (self.field is None) == (self.pattern is None):
            raise ValueError("deadline needs exactly one of field or pattern")  # raise-ok: pydantic validator (config loading)
        return self


class ExpectIn(Strict):
    """The item's references (of ``reference``, else every pattern) should appear in ``source``."""
    source: str
    reference: str | None = None
    _s = _short("source", as_list=False)


EFFECTS = ("importance", "urgency", "surface", "route", "area", "topic", "sensitivity", "deadline", "tags",
           "dismiss", "assess", "reason", "expect_in")


class Then(Strict):
    importance: int | None = None
    urgency: int | None = None
    surface: Literal["always"] | None = None
    route: str | None = None
    area: str | None = None
    topic: str | None = None
    sensitivity: str | None = None
    deadline: DeadlineEff | None = None
    tags: list[str] = []
    dismiss: str | None = None
    assess: Literal["rules_only"] | None = None
    reason: str | None = None
    expect_in: ExpectIn | None = None
    _l = _lists("tags")


class Rule(Strict):
    id: str
    description: str | None = None
    when: When
    then: Then


class ReferencePattern(Strict):
    """An identifier pattern; the value is the named group ``id``, else the whole match."""
    pattern: str
    fields: list[str] = ["title", "text", "url", "identity"]
    _s = _short("pattern", as_list=False)
    _l = _lists("fields")


# --- scoring and budgets (TD §8.2) -----------------------------------------------


class Caps(Strict):
    """Per-source caps: keep the top N by score; ``default`` applies to unnamed sources."""
    per_source: dict[str, int] = {}


class Budgets(Strict):
    questions: int | None = Field(None, ge=0)
    review_pool: int | None = Field(None, ge=0)
    daily: int | None = Field(None, ge=0)   # total kept items across sources; always_keep and must-surface bypass


class Scoring(Strict):
    formula: Literal["weighted", "max", "plugin"] = "weighted"
    plugin: str | None = None
    weights: dict[Literal["importance", "urgency", "agent"], float] = {"importance": 1.0, "urgency": 1.0,
                                                                       "agent": 1.0}
    base: int = Field(50, ge=0, le=100)
    floor: int = Field(0, ge=0, le=100)
    always_keep: int | None = Field(None, ge=0, le=100)
    surface_floor: int | None = Field(None, ge=0, le=100)
    caps: Caps = Caps()
    budget: Budgets | None = None

    @field_validator("weights")
    @classmethod
    def _finite(cls, v: dict[str, float]) -> dict[str, float]:
        for k, w in v.items():
            if not math.isfinite(w) or w < 0:
                raise ValueError(f"weight {k} must be a finite number >= 0")  # raise-ok: pydantic validator (config loading)
        if v and not any(w > 0 for w in v.values()):
            raise ValueError("at least one weight must be positive")  # raise-ok: pydantic validator (config loading)
        return v


class SignalsConfig(Strict):
    rules: list[Rule] = []
    references: dict[str, ReferencePattern] = {}
    scoring: Scoring = Scoring()
    budgets: Budgets = Budgets()
    units: Any = None   # modelled and checked in docket.config.source_models

    @property
    def effective_budgets(self) -> Budgets:
        base = (self.scoring.budget or Budgets()).model_dump()
        over = self.budgets.model_dump(exclude_unset=True)
        return Budgets.model_validate({**base, **{k: v for k, v in over.items() if v is not None}})


def parse(raw: Any) -> SignalsConfig:
    return SignalsConfig.model_validate(raw or {})


def signals_config(ws: Any) -> SignalsConfig:
    """The parsed ``signals`` section of a loaded (valid) workspace, cached on it."""
    cached = getattr(ws, "_signals_cfg", None)
    if cached is None:
        cached = parse(ws.config.signals)
        ws._signals_cfg = cached
    return cached


def rule_origins(ws: Any, cfg: SignalsConfig) -> list[str]:
    """``file:line`` (with its layer) of each rule, for traces and ``item why``."""
    out = []
    for i in range(len(cfg.rules)):
        org = ws.origins.get(("signals", "rules", i)) if getattr(ws, "origins", None) else None
        out.append(org.label() if org else "core")
    return out
