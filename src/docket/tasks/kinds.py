"""Task kinds (TD §9.1): one registry of input model, output model and instructions.

A kind declares its output as a tuple of ``FieldSpec`` from a closed field-type
set and a tuple of cross-field ``Rule``. ``build_models`` turns the declaration
into Pydantic models; the prompt's output specification (``docket.tasks.prompts``),
the validator (``docket.tasks.submit``) and ``docket schema <kind>-output`` all
read those models, so they cannot drift (FR-TSK-02, TD §20.1).

Core kinds (``assess``, ``extract``, ``review``, ``classify``) and workspace
kinds (``tasks.kinds``) go through the same builder. Workspace vocabulary
(extra item kinds, denylists, classes, guidance) comes from configuration.

Tolerant coercions are declared on the fields: a numeric string becomes an
integer, a single quote becomes a one-quote evidence list, a date-time becomes
its date. An open vocabulary value outside the declared set takes the field's
safe reading and records an unexpected shape; it never refuses (FR-TSK-04).
"""
from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from docket.config.digest import canonical_json, digest
from docket.config.task_models import CORE_KIND_NAMES, TasksConfig
from docket.shapes.unexpected import unexpected
from docket.text.templates import FieldSet

if TYPE_CHECKING:
    from docket.config.models import DocketConfig

CORE_KINDS = CORE_KIND_NAMES

# The template context of kind instructions and guidance.
INSTRUCTIONS_CONTEXT = FieldSet(["me.name", "kind", "stage"])

ITEM_KINDS: dict[str, str] = {
    "request": "Someone asks the operator to do, answer or provide something.",
    "commitment": "The operator, or someone they are responsible for, promised to do something.",
    "decision": "A decision the operator must make or weigh in on.",
    "risk": "Something will likely go wrong unless someone acts.",
    "fyi": "Worth knowing; nothing is owed.",
}
ITEM_KIND_DENY = ("other", "misc", "unknown", "none", "n/a", "na")
ASK_DENY = ("maybe", "perhaps", "possibly", "fyi")
ASK_DEFAULT_VERB = "Follow up on"
VERDICTS: dict[str, str] = {
    "ask": "Put it to the operator as a question.",
    "merge": "It duplicates another candidate in this pack; name that one in merge_with.",
    "drop": "Not worth the operator's time: unsupported, already handled, or busywork.",
    "needs_verification": "The pack cannot settle it; flag it for verification before asking.",
}
EXTRACT_KINDS: dict[str, str] = {
    "request": "Someone asked for something to be done.",
    "commitment": "Someone promised to do something.",
}
CLASSES: dict[str, str] = {
    "direct": "A one-to-one conversation with the operator.",
    "team": "A small working group the operator belongs to.",
    "broadcast": "A large audience; the operator is one reader of many.",
    "automated": "Messages written by a system, not a person.",
    "unclassified": "None of the classes fits.",
}


# ---------------------------------------------------------------------------
# Declarations


@dataclass(frozen=True)
class FieldSpec:
    """One output field. ``type`` is one of the closed field types, or ``rows`` (core only)."""
    name: str
    type: str
    description: str
    required: bool = False
    values: tuple[tuple[str, str], ...] = ()     # enum: (value, meaning), in prompt order
    safe: str | None = None                       # enum: reading of an unknown or denied value
    deny: tuple[str, ...] = ()                    # enum: values read as safe; str: denied leading words
    default_verb: str | None = None               # str with deny: the replacement leading word(s)
    max_length: int | None = None
    max_items: int = 3                            # evidence: quotes per row
    max_quote: int = 300                          # evidence: characters per quote
    lo: int = 0
    hi: int = 100
    echo: str | None = None                       # copied exactly from this pack item field
    rows: tuple["FieldSpec", ...] = ()            # rows: the sub-row fields
    example: Any = None
    extend: str = ""                              # where an unknown vocabulary value is modelled

    @property
    def attr(self) -> str:
        return self.name + "_" if keyword.iskeyword(self.name) or not self.name.isidentifier() else self.name

    def value_names(self) -> list[str]:
        return [v for v, _ in self.values]


@dataclass(frozen=True)
class Rule:
    """When every ``when`` field equals its value, every ``require`` field must be set."""
    when: tuple[tuple[str, Any], ...]
    require: tuple[str, ...]
    reason: str = ""

    def applies(self, row: dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in self.when)

    def missing(self, row: dict[str, Any]) -> list[str]:
        if not self.applies(row):
            return []
        return [f for f in self.require if row.get(f) in (None, "", [], {})]

    def describe(self) -> str:
        cond = " and ".join(f"`{k}` is {_lit(v)}" for k, v in self.when)
        req = _join([f"`{f}`" for f in self.require])
        verb = "is" if len(self.require) == 1 else "are"
        return f"When {cond}: {req} {verb} required." + (f" ({self.reason})" if self.reason else "")

    def as_dict(self) -> dict[str, Any]:
        return {"when": dict(self.when), "require": list(self.require), "reason": self.reason}


def _lit(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f"`{v}`"
    return str(v)


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


@dataclass(frozen=True)
class Select:
    sources: tuple[str, ...] | None = None
    units: tuple[str, ...] | None = None
    kept: bool | None = None

    def matches(self, it: dict[str, Any]) -> bool:
        if self.sources is not None and it.get("source") not in self.sources:
            return False
        if self.units is not None and it.get("unit") not in self.units:
            return False
        if self.kept is not None and bool(it.get("keep")) != self.kept:
            return False
        return True


@dataclass(frozen=True)
class TaskKind:
    name: str
    stage: str                    # the delegated stage that issues it
    core: bool
    description: str
    instructions: str             # a template over INSTRUCTIONS_CONTEXT
    guidance: str | None
    fields: tuple[FieldSpec, ...]
    rules: tuple[Rule, ...]
    max_items: int
    max_bytes: int
    select: Select | None         # None: the kind is not issued (extract, classify unconfigured)
    view: str                     # pack item view: "item" or "review" (assessment shown, no score)
    row_model: type[BaseModel] = field(compare=False, repr=False, default=BaseModel)
    result_model: type[BaseModel] = field(compare=False, repr=False, default=BaseModel)
    pack_model: type[BaseModel] = field(compare=False, repr=False, default=BaseModel)
    spec_digest: str = ""

    @property
    def input_schema(self) -> str:
        return f"{self.name}-input"

    @property
    def output_schema(self) -> str:
        return f"{self.name}-output"

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


# ---------------------------------------------------------------------------
# Coercions (declared tolerant readings)


def _strip(v: Any) -> Any:
    return v.strip() if isinstance(v, str) else v


def _coerce_int(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v == v and abs(v) < 1e9:
        return int(round(v))
    if isinstance(v, str):
        s = v.strip().rstrip("%").strip()
        try:
            return int(round(float(s)))
        except ValueError:
            return v
    return v


def _coerce_date(spec: FieldSpec, kind: str, v: Any) -> Any:
    if v is None or isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None if not spec.required else v
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    if spec.required:
        return v
    unexpected("task.vocabulary", f"{kind}.{spec.name} not a date", sample=str(v)[:80],
               reading="no date", extend=f"{spec.extend or kind + '.' + spec.name}: date formats")
    return None


def _coerce_duration(spec: FieldSpec, kind: str, v: Any) -> Any:
    if v is None or (isinstance(v, int) and not isinstance(v, bool)):
        return v
    from docket.cli.errors import InputError
    from docket.text.durations import parse_duration
    try:
        return parse_duration(v)
    except InputError:
        pass
    if spec.required:
        return v
    unexpected("task.vocabulary", f"{kind}.{spec.name} not a duration", sample=str(v)[:80],
               reading="no duration", extend=f"{spec.extend or kind + '.' + spec.name}: duration formats")
    return None


def _coerce_enum(spec: FieldSpec, kind: str, v: Any) -> Any:
    if v is None:
        return v
    if not isinstance(v, (str, int, float, bool)):
        return v
    s = str(v).strip()
    names = spec.value_names()
    low = s.lower().replace(" ", "_").replace("-", "_")
    for n in names:
        if s == n or low == n.lower().replace("-", "_"):
            return n
    if spec.safe is None:
        return v
    why = "denied" if low in spec.deny else "unknown"
    unexpected("task.vocabulary", f"{kind}.{spec.name} {why} value", sample=s[:80],
               reading=f"read as {spec.safe}", extend=f"{spec.extend or kind + '.' + spec.name}: values")
    return spec.safe


def _coerce_str(spec: FieldSpec, kind: str, v: Any) -> Any:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return v
    s = " ".join(v.split())
    if not s:
        return None if not spec.required else s
    if spec.deny and spec.default_verb:
        first, _, rest = s.partition(" ")
        if first.lower().strip(".,:;!") in spec.deny:
            unexpected("task.vocabulary", f"{kind}.{spec.name} denied leading word", sample=s[:80],
                       reading=f"start with {spec.default_verb!r}", extend=f"{spec.extend or 'tasks.' + kind}: deny")
            s = f"{spec.default_verb} {rest}".strip() if rest else spec.default_verb
    return s


def _coerce_evidence(v: Any) -> Any:
    if v is None:
        return v
    if isinstance(v, (str, dict)):
        v = [v]
    if isinstance(v, list):
        out = []
        for q in v:
            if isinstance(q, str):
                out.append({"quote": q})
            elif isinstance(q, dict) and "quote" not in q and "text" in q:
                out.append({**{k: x for k, x in q.items() if k != "text"}, "quote": q["text"]})
            else:
                out.append(q)
        return out
    return v


def _coercer(spec: FieldSpec, kind: str) -> Any:
    t = spec.type
    if t == "int":
        return lambda cls, v: _coerce_int(v)
    if t == "date":
        return lambda cls, v: _coerce_date(spec, kind, v)
    if t == "duration":
        return lambda cls, v: _coerce_duration(spec, kind, v)
    if t == "enum":
        return lambda cls, v: _coerce_enum(spec, kind, v)
    if t in ("str", "item_ref"):
        return lambda cls, v: _coerce_str(spec, kind, v)
    if t == "evidence":
        return lambda cls, v: _coerce_evidence(v)
    if t == "bool":
        return lambda cls, v: v.strip().lower() if isinstance(v, str) else v
    return None


# ---------------------------------------------------------------------------
# Model building


def _evidence_model(kind: str, max_quote: int) -> type[BaseModel]:
    return create_model(
        f"{_camel(kind)}Evidence",
        __config__=ConfigDict(extra="ignore"),
        __doc__="One verbatim quote and the pack line id it was copied from.",
        line=(Optional[str], Field(None, description="The id of the pack line the quote comes from, such as L2.")),
        quote=(str, Field(min_length=1, max_length=max_quote,
                          description="Words copied exactly from the item's lines; never paraphrased.")),
        __validators__={"_q": field_validator("quote", mode="before")(classmethod(lambda cls, v: _strip(v)))},
    )


def _camel(name: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", name) if p) or "Kind"


def _annotation(spec: FieldSpec, kind: str, prefix: str) -> tuple[Any, dict[str, Any]]:
    t = spec.type
    extra: dict[str, Any] = {"x-type": t}
    kw: dict[str, Any] = {}
    if t == "str" or t == "item_ref":
        tp: Any = str
        if spec.max_length:
            kw["max_length"] = spec.max_length
        if spec.required:
            kw["min_length"] = 1
    elif t == "int":
        tp = int
        kw.update(ge=spec.lo, le=spec.hi)
    elif t == "bool":
        tp = bool
    elif t == "enum":
        tp = Literal[tuple(spec.value_names())]  # type: ignore[misc]
        extra["x-meanings"] = dict(spec.values)
        if spec.safe:
            extra["x-safe"] = spec.safe
    elif t == "date":
        tp = date
    elif t == "duration":
        tp = int
        kw["ge"] = 0
        extra["x-unit"] = "minutes"
    elif t == "evidence":
        tp = list[_evidence_model(kind, spec.max_quote)]  # type: ignore[misc]
        kw["max_length"] = spec.max_items
        if spec.required:
            kw["min_length"] = 1
    elif t == "rows":
        sub = _row_model(f"{prefix}{_camel(spec.name)}", kind, spec.rows, (), with_id=False)
        tp = list[sub]  # type: ignore[valid-type]
    else:  # pragma: no cover - the field type set is closed
        raise ValueError(f"unknown field type {t}")
    if spec.echo:
        extra["x-echo"] = spec.echo
    return tp, {"json_schema_extra": extra, **kw}


def _row_model(name: str, kind: str, fields: tuple[FieldSpec, ...], rules: tuple[Rule, ...],
               *, with_id: bool) -> type[BaseModel]:
    defs: dict[str, Any] = {}
    validators: dict[str, Any] = {}
    if with_id:
        defs["id"] = (str, Field(min_length=1, description="The pack item id, copied exactly.",
                                 json_schema_extra={"x-type": "item_ref", "x-echo": "id"}))
        validators["_id"] = field_validator("id", mode="before")(classmethod(lambda cls, v: _strip(v)))
    for f in fields:
        tp, kw = _annotation(f, kind, name)
        fd = Field(... if f.required else None, alias=f.name, description=f.description, **kw)
        defs[f.attr] = (tp if f.required else Optional[tp], fd)
        co = _coercer(f, kind)
        if co is not None:
            validators[f"_c_{f.attr}"] = field_validator(f.attr, mode="before")(classmethod(co))
    schema_extra = {"x-rules": [r.describe() for r in rules]} if rules else None
    model = create_model(
        name, __config__=ConfigDict(extra="allow", populate_by_name=True, serialize_by_alias=True,
                                     json_schema_extra=schema_extra),
        __validators__=validators, **defs,
    )
    model.__docket_fields__ = fields  # type: ignore[attr-defined]
    model.__docket_rules__ = rules  # type: ignore[attr-defined]
    return model


def build_models(kind: TaskKind) -> TaskKind:
    from docket.tasks.packs import pack_model
    row = _row_model(f"{_camel(kind.name)}Row", kind.name, kind.fields, kind.rules, with_id=True)
    result = create_model(
        f"{_camel(kind.name)}Output",
        __config__=ConfigDict(extra="allow"),
        __doc__=f"The result of a {kind.name} task: exactly one row per pack item.",
        items=(list[row], Field(description="One row per pack item id; no missing, extra or repeated ids.")),  # type: ignore[valid-type]
    )
    pm = pack_model(kind.name)
    spec = digest({"instructions": kind.instructions, "guidance": kind.guidance,
                   "output": result.model_json_schema(), "rules": [r.as_dict() for r in kind.rules],
                   "view": kind.view})
    object.__setattr__(kind, "row_model", row)
    object.__setattr__(kind, "result_model", result)
    object.__setattr__(kind, "pack_model", pm)
    object.__setattr__(kind, "spec_digest", spec)
    return kind


# ---------------------------------------------------------------------------
# Core kinds

ASSESS_INSTRUCTIONS = """\
You assess items collected for {me.name}, the operator. For each item in the pack, decide whether it needs the operator's attention and, if it does, what they should do.

- Keep an item when it asks the operator for something, records a commitment the operator or someone they are responsible for made, needs a decision from them, or signals a risk they should act on.
- Do not keep an item that only informs, that someone else has already handled, or whose only work would be recovering missing context.
- Judge each item from its own lines, its unit and the profile excerpts in the pack. Do not assume facts the pack does not contain.
- Score from 0 to 100 by how much the item deserves the operator's attention today. A kept item usually scores above 50.
- Write the ask as one imperative line that names the action and its object.
- Quote evidence word for word from the item's lines and copy each quote's line id. Never paraphrase, shorten inside a quote, or translate."""

REVIEW_INSTRUCTIONS = """\
You review candidate findings before they are put to {me.name}, the operator. An earlier assessment kept each candidate; the pack shows its proposal (kind, owner, ask, evidence) without its score. Form your own view.

Hunt for these failures:
1. Unsupported claim: the quotes are real but do not support the ask.
2. Already handled: the lines show the request was answered, delegated or resolved.
3. Duplicate: two candidates describe one piece of work. Merge the weaker into the stronger.
4. Vague ask: the ask names no concrete action, owner or object.
5. Busywork: missing context was turned into work to recover it. Missing context is not an obligation.

For every candidate you leave at ask, write the strongest argument against asking in one sentence. Use needs_verification when the pack cannot settle the question."""

EXTRACT_INSTRUCTIONS = """\
You extract commitments and requests from documents collected for {me.name}, the operator. Each pack item is one document; its lines are the document's sections.

- Return every commitment (someone promised to do something) and every request (someone asked for something) the document records, one entry each.
- Name the owner: me for the operator, a person id from the pack's profile, or the name as the document writes it.
- Quote evidence word for word from the document's lines and copy each quote's line id. Never paraphrase.
- Return an empty entries list for a document that records none."""

CLASSIFY_INSTRUCTIONS = """\
You classify entries collected for {me.name}, the operator. For each pack item choose the class that best describes it, and a tier from 1 (most important to the operator) to 9 when the entry suggests one.

- Judge from the entry's lines, its unit and the profile excerpts in the pack.
- Give a one-sentence reason that names what in the entry decided the class."""


def _assess(cfg: TasksConfig) -> TaskKind:
    a = cfg.assess
    kinds = {**ITEM_KINDS, **a.kinds}
    deny = tuple(sorted({*ITEM_KIND_DENY, *(d.lower() for d in a.deny_kinds)} - {k.lower() for k in a.kinds}))
    ask_deny = tuple(sorted({*ASK_DENY, *(d.lower() for d in a.ask.deny)}))
    fields = (
        FieldSpec("source", "str", "The pack item's source, copied exactly.", required=True, echo="source"),
        FieldSpec("keep", "bool", "true when the operator should be asked about the item; false when it needs "
                                  "nothing from them.", required=True, example=True),
        FieldSpec("score", "int", "How much the item deserves the operator's attention today, 0 to 100.",
                  required=True, example=70),
        FieldSpec("kind", "enum", "What the item is.", values=tuple(kinds.items()), safe="fyi", deny=deny,
                  example="request", extend="tasks.assess.kinds"),
        FieldSpec("owner", "str", "Who must act: me for the operator, a person id from the pack's profile, "
                                  "or the name as the item writes it.", max_length=120, example="me"),
        FieldSpec("ask", "str", "One imperative line: what the operator should do, starting with a verb.",
                  max_length=200, deny=ask_deny, default_verb=a.ask.default_verb or ASK_DEFAULT_VERB,
                  example="Reply with the decision the item asks for", extend="tasks.assess.ask"),
        FieldSpec("evidence", "evidence", "Quotes copied exactly from the item's lines, each with its line id.",
                  max_items=a.max_evidence, max_quote=a.max_quote),
        FieldSpec("deadline", "date", "The date the ask is due (YYYY-MM-DD), only when the item states or "
                                      "clearly implies one.", extend="tasks.assess"),
        FieldSpec("topic", "str", "A topic id from the pack's profile when one matches, else a short noun "
                                  "phrase naming the subject.", max_length=80),
    )
    rules = (Rule((("keep", True),), ("owner", "ask", "evidence"),
                  "a kept item becomes a question: it needs who acts, what to do and the words that show it"),)
    return TaskKind("assess", "assess", True, "Decide which items need the operator and what they should do.",
                    ASSESS_INSTRUCTIONS, a.guidance, fields, rules, a.max_items, a.max_bytes, Select(), "item")


def _review(cfg: TasksConfig) -> TaskKind:
    r = cfg.review
    fields = (
        FieldSpec("verdict", "enum", "Your verdict on asking the operator about this candidate.", required=True,
                  values=tuple(VERDICTS.items()), safe="needs_verification", example="ask",
                  extend="docket/tasks/kinds.py: VERDICTS"),
        FieldSpec("against", "str", "The strongest argument against asking the operator, in one sentence.",
                  max_length=300, example="The thread may already hold the answer the item asks for"),
        FieldSpec("merge_with", "item_ref", "The id of the pack item this candidate duplicates."),
        FieldSpec("reason", "str", "One sentence naming why, and which hunted failure fired when one did.",
                  max_length=300),
    )
    rules = (
        Rule((("verdict", "ask"),), ("against",), "the operator sees the case against with every question"),
        Rule((("verdict", "merge"),), ("merge_with",)),
        Rule((("verdict", "drop"),), ("reason",)),
        Rule((("verdict", "needs_verification"),), ("reason",)),
    )
    return TaskKind("review", "review", True, "Check each kept candidate before the operator is asked.",
                    REVIEW_INSTRUCTIONS, r.guidance, fields, rules, r.max_items, r.max_bytes, Select(kept=True),
                    "review")


def _extract(cfg: TasksConfig) -> TaskKind:
    e = cfg.extract
    entry = (
        FieldSpec("kind", "enum", "Whether the entry is a request or a commitment.", required=True,
                  values=tuple(EXTRACT_KINDS.items()), safe="request", example="commitment",
                  extend="docket/tasks/kinds.py: EXTRACT_KINDS"),
        FieldSpec("owner", "str", "Who must act: me for the operator, a person id from the pack's profile, "
                                  "or the name as the document writes it.", required=True, max_length=120,
                  example="me"),
        FieldSpec("ask", "str", "One imperative line: what the owner should do.", required=True, max_length=200,
                  deny=ASK_DENY, default_verb=ASK_DEFAULT_VERB, example="Send the summary the meeting agreed on",
                  extend="tasks.extract"),
        FieldSpec("evidence", "evidence", "Quotes copied exactly from the document's lines, each with its line id.",
                  required=True, max_quote=e.max_quote),
        FieldSpec("deadline", "date", "The date it is due (YYYY-MM-DD), only when the document states one.",
                  extend="tasks.extract"),
    )
    fields = (
        FieldSpec("source", "str", "The pack item's source, copied exactly.", required=True, echo="source"),
        FieldSpec("entries", "rows", "Every commitment and request the document records; empty when none.",
                  required=True, rows=entry),
    )
    sel = Select(tuple(e.select.sources) if e.select.sources is not None else None,
                 tuple(e.select.units) if e.select.units is not None else None) if e.select else None
    return TaskKind("extract", "assess", True, "List the commitments and requests a document records.",
                    EXTRACT_INSTRUCTIONS, e.guidance, fields, (), e.max_items, e.max_bytes, sel, "item")


def _classify(cfg: TasksConfig) -> TaskKind:
    c = cfg.classify
    classes = c.classes or CLASSES
    safe = c.safe or ("unclassified" if "unclassified" in classes else next(iter(classes)))
    fields = (
        FieldSpec("class", "enum", "The class that best describes the entry.", required=True,
                  values=tuple(classes.items()), safe=safe, example=next(iter(classes)), extend="tasks.classify.classes"),
        FieldSpec("tier", "int", "Importance to the operator, 1 (most) to 9 (least), when the entry suggests one.",
                  lo=1, hi=9, example=2),
        FieldSpec("reason", "str", "One sentence naming what in the entry decided the class.", required=True,
                  max_length=300, example="Only two people take part in the conversation"),
    )
    sel = Select(tuple(c.select.sources) if c.select.sources is not None else None,
                 tuple(c.select.units) if c.select.units is not None else None) if c.select else None
    return TaskKind("classify", "assess", True, "Give each entry a class and a tier.",
                    CLASSIFY_INSTRUCTIONS, c.guidance, fields, (), c.max_items, c.max_bytes, sel, "item")


def _custom(name: str, kc: Any) -> TaskKind:
    fields = []
    for fname, fc in kc.fields.items():
        fields.append(FieldSpec(
            fname, fc.type, fc.description, required=fc.required, values=tuple(fc.values.items()),
            safe=fc.safe, deny=tuple(d.lower() for d in fc.deny), max_length=fc.max_length,
            max_items=fc.max_items, extend=f"tasks.kinds.{name}.fields.{fname}"))
    rules = tuple(Rule(tuple(r.when.items()), tuple(r.require), r.reason or "") for r in kc.rules)
    s = kc.select
    sel = Select(tuple(s.sources) if s.sources is not None else None,
                 tuple(s.units) if s.units is not None else None, s.kept)
    return TaskKind(name, kc.stage, False, kc.description or f"Workspace task kind {name}.", kc.instructions,
                    None, tuple(fields), rules, kc.max_items, kc.max_bytes, sel,
                    "review" if kc.stage == "review" else "item")


@lru_cache(maxsize=32)
def _registry(key: bytes) -> dict[str, TaskKind]:
    import json
    cfg = TasksConfig.model_validate(json.loads(key))
    kinds = [_assess(cfg), _extract(cfg), _review(cfg), _classify(cfg)]
    kinds += [_custom(n, kc) for n, kc in sorted(cfg.kinds.items())]
    return {k.name: build_models(k) for k in kinds}


def registry(tasks: TasksConfig | None = None) -> dict[str, TaskKind]:
    """Every kind (core first, then workspace kinds by name), built for this ``tasks`` configuration."""
    cfg = tasks if tasks is not None else TasksConfig()
    return _registry(canonical_json(cfg.model_dump(mode="json")))


def kinds_for(config: "DocketConfig") -> dict[str, TaskKind]:
    return registry(config.tasks)


def stage_kinds(config: "DocketConfig", stage: str) -> list[TaskKind]:
    """Kinds a delegated stage issues: its core kind first, then the others it issues, by name."""
    reg = kinds_for(config)
    out = [k for k in reg.values() if k.stage == stage and k.select is not None]
    return sorted(out, key=lambda k: (k.name != stage, not k.core, k.name))


def schemas(tasks: TasksConfig | None = None) -> dict[str, type[BaseModel]]:
    """``<kind>-input`` and ``<kind>-output`` for every kind, for ``docket schema``."""
    out: dict[str, type[BaseModel]] = {}
    for k in registry(tasks).values():
        out[k.input_schema] = k.pack_model
        out[k.output_schema] = k.result_model
    return out
