"""Configuration models (TD §3.2).

Every top-level section is a Pydantic model with ``extra="forbid"``. Sections
whose full content belongs to later milestones (signals) are typed as open
maps for now; ``shapes`` holds reviewed shape acceptances (TD §14.2);
``journal`` is modelled in ``docket.config.journal_models``; the top-level structure, ``profile``,
``graphs``, ``sources``, ``transports``, ``decisions``, ``destinations``,
``planning`` and ``tasks`` are strict. ``sources``, ``transports`` and
``signals.units`` are modelled in ``docket.config.source_models``; ``tasks`` in
``docket.config.task_models``.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from docket.config.source_models import MAPPING_KEY_DOCS, Source, TransportConfig  # noqa: F401  (sections owned there)
from docket.config.task_models import TasksConfig  # M4: the tasks section (TD §9)
from docket.config.journal_models import JournalConfig  # the journal section (TD §13.5)

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SOURCE_KINDS: dict[str, frozenset[str]] = {
    # kind (the transport kind) -> capabilities the transport can serve (TD §6.3).
    "files": frozenset({"read", "context"}),
    "mcp_stdio": frozenset({"read", "discover", "resolve", "context"}),
    "mcp_http": frozenset({"read", "discover", "resolve", "context"}),
    "codemode": frozenset({"read", "discover", "resolve", "context"}),
    "exec_jsonl": frozenset({"read", "discover", "resolve", "context"}),
    "http_json": frozenset({"read", "discover", "resolve", "context"}),
    "replay": frozenset({"read", "discover", "resolve", "context"}),
}
IMPLEMENTED_SOURCE_KINDS = frozenset(SOURCE_KINDS)

DESTINATION_ADAPTERS: dict[str, frozenset[str]] = {
    # adapter -> actions it can accept
    "markdown_tasks": frozenset({"task.add"}),
    "markdown_agenda": frozenset({"agenda.add"}),
    "journal": frozenset(),
    "calendar_exec": frozenset({"calendar.place"}),
    "calendar_file": frozenset({"calendar.place"}),
    "calendar_mcp": frozenset({"calendar.place"}),
    "message_mcp": frozenset({"message.draft"}),
}
IMPLEMENTED_ADAPTERS = frozenset(DESTINATION_ADAPTERS)

ACTIONS = ("task.add", "agenda.add", "calendar.place", "message.draft", "now", "done", "watch", "ack", "dismiss")
ACTION_NEEDS_DESTINATION = {"task.add", "agenda.add", "calendar.place", "message.draft"}
WHEN_GUARDS = ("has_forum", "has_reply_channel", "has_deadline", "has_calendar", "source_is", "relation_is")

MAPPING_KEYS = tuple(MAPPING_KEY_DOCS)  # item fields, then the M3 structure keys


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- profile ---------------------------------------------------------------


class Me(Strict):
    name: str
    aliases: list[str] = []
    handles: dict[str, str] = {}
    reports_to: str | None = None


class Person(Strict):
    id: str
    name: str
    aliases: list[str] = []
    handles: dict[str, str] = {}
    reports_to: str | None = None
    roles: list[str] = []


class Group(Strict):
    id: str
    name: str | None = None
    members: list[str] = []
    forum: str | None = None


class Topic(Strict):
    id: str
    aliases: list[str] = []
    area: str | None = None
    owned: bool = False
    forum: str | None = None        # the forum where this topic is discussed


class Entity(Strict):
    id: str
    aliases: list[str] = []
    protected: bool = False
    strategic: bool = False


class Forum(Strict):
    id: str
    name: str | None = None
    kind: str = "group"
    person: str | None = None       # a meeting with one person: that person's profile id
    agenda: dict[str, Any] | None = None
    calendar_match: dict[str, Any] | None = None


CALENDAR_KINDS = ("meeting", "focus", "soft_hold", "out_of_office", "boundary", "mirrored", "owned",
                  "informational")
CalendarKind = Literal["meeting", "focus", "soft_hold", "out_of_office", "boundary", "mirrored", "owned",
                       "informational"]


class ClassifyMatch(Strict):
    """Every key given must hold (TD §12.1). ``property`` values: ``true`` means present and
    truthy, ``false`` absent or falsy, anything else equality (case-insensitive for text)."""
    title_regex: str | None = None
    property: dict[str, Any] = {}
    calendar: str | None = None
    transparency: Literal["opaque", "transparent"] | None = None
    all_day: bool | None = None


class ClassifyRule(Strict):
    match: ClassifyMatch
    kind: CalendarKind


class CalendarVocabulary(Strict):
    classify: list[ClassifyRule] = []


class Profile(Strict):
    timezone: str = "UTC"
    workdays: list[Weekday] = ["mon", "tue", "wed", "thu", "fri"]
    internal_domains: list[str] = []
    me: Me
    people: list[Person] = []
    groups: list[Group] = []
    topics: list[Topic] = []
    entities: list[Entity] = []
    forums: list[Forum] = []
    calendar: CalendarVocabulary = CalendarVocabulary()


# --- graphs (TD §5) -----------------------------------------------------------

VERBS: tuple[str, ...] = ("read", "run", "judge", "merge", "ask", "plan", "approve", "apply", "write")
# What a node hands on: items, the operator's decisions, labels, a plan, an approval, or nothing.
OUTPUT: dict[str, str] = {"read": "items", "judge": "items", "merge": "items", "ask": "decisions", "plan": "plan",
                          "approve": "approval", "apply": "done", "write": "done", "run": "done"}


class CoveredQuestions(Strict):
    """Where covered questions come from (TD §10.4): the read node of the meetings, the agenda
    destination the answers roll, and the suffix of the dated heading that says there was no record."""
    meetings: str
    to: str
    suffix: str = "no record of the meeting"


class Questions(Strict):
    kinds: list[str] = ["finding"]
    covered: CoveredQuestions | None = None
    bursts: int = 1
    # How many findings are asked one by one, and how many recommended for a batch option.
    # None (null) means no limit.
    budget: int | None = Field(None, ge=0)
    # A must-surface finding and the budget: left to its score, asked first within the
    # budget, or asked first without using it.
    must_surface: Literal["ranked", "first", "bypass"] = "ranked"
    # Items recommended for a batch option are ranked and cut in their own group.
    batch_budget: int | None = Field(None, ge=0)
    # The findings past a budget: still asked, flagged overflow and after the rest (list),
    # or not asked and reported as not_asked (drop).
    overflow: Literal["list", "drop"] = "list"

    @field_validator("kinds")
    @classmethod
    def _known_kinds(cls, v: list[str]) -> list[str]:
        for k in v:
            if k not in QUESTION_KINDS:
                raise ValueError(f"unknown question kind {k!r}; kinds are {list(QUESTION_KINDS)}")
        return v

    @model_validator(mode="after")
    def _covered(self) -> "Questions":
        if "covered" in self.kinds and self.covered is None:
            raise ValueError("the covered kind needs questions.covered: the read node of the meetings (meetings) "
                             "and the agenda destination (to)")
        if self.covered is not None and "covered" not in self.kinds:
            raise ValueError("questions.covered is read only when kinds lists covered")
        return self


class ReadSchedule(Strict):
    """How often a source is read. A day off the schedule is skipped, never a gap."""
    weekdays: list[Weekday] | None = None   # read only on these days
    every_days: int | None = Field(None, ge=1)   # read at most every N days

    @model_validator(mode="after")
    def _one(self) -> "ReadSchedule":
        if not self.weekdays and not self.every_days:
            raise ValueError("a schedule needs weekdays or every_days")
        return self


class Node(Strict):
    """One node of a graph (TD §5.1). It does one thing, named by its verb:

    - ``read: <source>``: read a source.
    - ``run: [argv]``: run a command (its ``dry_run`` form in a sandbox run).
    - ``judge: <task>``: have an agent judge what ``from`` hands it.
    - ``merge: [nodes]``: combine rules, scores and judgments into what is worth showing.
    - ``ask: <option set>``: the operator decides each item ``from`` hands it.
    - ``plan: [nodes]``: turn decisions into operations.
    - ``approve: <plan node>``: the operator approves the plan.
    - ``apply: <approve node>``: write the approved operations.
    - ``write: <action>``: plan and write operations that need no approval; only a private
      destination can be written this way.

    ``from`` names the nodes whose output it works on; ``after`` names nodes that must be
    done first and hand it nothing.
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)
    read: str | None = None
    run: list[str] | None = None
    judge: str | None = None
    merge: list[str] | None = None
    ask: str | None = None
    plan: list[str] | None = None
    approve: str | None = None
    apply: str | None = None
    write: str | None = None
    from_: list[str] = Field(default_factory=list, alias="from")
    after: list[str] = []
    description: str | None = None
    # read
    schedule: ReadSchedule | None = None
    # run
    dry_run: list[str] | None = None
    env: list[str] = []
    timeout_s: float = Field(120.0, gt=0, le=3600)
    # ask
    questions: Questions = Questions()
    # plan
    schedule_tasks: bool = False     # also place open tasks on the calendar (TD §12)
    # write
    to: str | None = None            # the destination written
    # A meeting with no record: with no_record, assume its agenda was covered and roll it all
    # under a heading that says this; without, leave it alone (a covered question may ask).
    no_record: str | None = None
    trailer: str = ""
    commitments: str | None = None   # a JSON-lines file for what the operator agreed to do (TD §12.8)

    @model_validator(mode="after")
    def _one_verb(self) -> "Node":
        verbs = [v for v in VERBS if getattr(self, v) is not None]
        if len(verbs) != 1:
            raise ValueError("a node has exactly one verb: " + ", ".join(VERBS))
        v = verbs[0]
        if v in ("merge", "plan", "approve", "apply") and self.from_:
            raise ValueError(f"{v} names its inputs itself; it takes no from")
        if v in ("read", "run") and self.from_:
            raise ValueError(f"{v} works on nothing another node hands it; use after to order it")
        if v == "write" and not self.to:
            raise ValueError("write needs to: the destination it writes")
        if self.schedule is not None and v != "read":
            raise ValueError("schedule says how often a source is read; only a read node takes one")
        return self

    @property
    def verb(self) -> str:
        return next(v for v in VERBS if getattr(self, v) is not None)

    @property
    def inputs(self) -> list[str]:
        """The nodes whose output this node works on."""
        v = self.verb
        if v in ("merge", "plan"):
            return list(getattr(self, v))
        if v in ("approve", "apply"):
            return [getattr(self, v)]
        return list(self.from_)

    @property
    def needs(self) -> list[str]:
        """What the node waits for: its inputs, ``after``, and the read node its covered
        questions take their meetings from."""
        covered = [self.questions.covered.meetings] if self.verb == "ask" and self.questions.covered else []
        return list(dict.fromkeys([*self.inputs, *self.after, *covered]))


def graph_problems(nodes: dict[str, Node]) -> list[str]:
    """What is wrong with a graph's shape: names, references, cycles, and inputs of the wrong kind."""
    out: list[str] = []
    for nid, n in nodes.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", nid) or nid == "seal":
            out.append(f"node name {nid!r} must be a lower-case word ([a-z][a-z0-9-]*) other than seal")
        for other in n.needs:
            if other not in nodes:
                out.append(f"node {nid} names {other!r}, which is not a node of the graph")
            elif other == nid:
                out.append(f"node {nid} names itself")
    if out:
        return out
    state: dict[str, int] = {}

    def visit(nid: str, trail: tuple[str, ...]) -> None:
        if state.get(nid) == 2 or out:
            return
        if state.get(nid) == 1:
            out.append("the graph has a cycle: " + " -> ".join((*trail[trail.index(nid):], nid)))
            return
        state[nid] = 1
        for other in nodes[nid].needs:
            visit(other, (*trail, nid))
        state[nid] = 2

    for nid in nodes:
        visit(nid, ())
    if out:
        return out
    want = {"merge": ("items",), "ask": ("items",), "approve": ("plan",), "apply": ("approval",), "write": ("items",),
            "plan": ("decisions", "items")}
    for nid, n in nodes.items():
        v = n.verb
        for other in n.inputs:
            got = OUTPUT[nodes[other].verb]
            ok = want.get(v) or (("items", "decisions") if v == "judge" else ())
            if got not in ok:
                out.append(f"node {nid} ({v}) cannot work on {other} ({nodes[other].verb}), which hands on "
                           f"{'nothing' if got == 'done' else got}")
        if v in ("merge", "approve", "apply", "write") and not n.inputs:
            out.append(f"node {nid} ({v}) needs a node to work on")
        if v == "plan" and sum(1 for o in n.inputs if nodes[o].verb == "ask") > 1:
            out.append(f"node {nid} (plan) takes the decisions of one ask node")
        cov = n.questions.covered if v == "ask" else None
        if cov is not None and nodes[cov.meetings].verb != "read":
            out.append(f"node {nid} (ask) reads the meetings of {cov.meetings} ({nodes[cov.meetings].verb}); "
                       f"questions.covered.meetings names a read node")
    return out


def graph_conflicts(nodes: dict[str, Node]) -> list[tuple[str, str]]:
    """Settings of one graph that ask for opposite things, as ``(node, message)``: a roll that
    assumes a meeting with no record covered its agenda (``no_record``) and an ask node that
    asks about the same meetings (``covered``). The same meetings are those of one source."""
    def sources_upstream(nid: str) -> set[str]:
        seen: set[str] = set()
        todo = [nid]
        out: set[str] = set()
        while todo:
            x = todo.pop()
            if x in seen or x not in nodes:
                continue
            seen.add(x)
            if nodes[x].verb == "read":
                out.add(str(nodes[x].read))
            todo += nodes[x].inputs
        return out

    asked = {nid: str(nodes[n.questions.covered.meetings].read) for nid, n in nodes.items()
             if n.verb == "ask" and n.questions.covered is not None and n.questions.covered.meetings in nodes
             and nodes[n.questions.covered.meetings].verb == "read"}
    out = []
    for wid, w in nodes.items():
        if w.verb != "write" or w.write != "roll" or w.no_record is None:
            continue
        mine = sources_upstream(wid)
        for aid, source in sorted(asked.items()):
            if source in mine:
                out.append((wid, f"node {wid} assumes a meeting with no record covered its agenda (no_record), and "
                                 f"node {aid} asks about the same meetings (covered); drop no_record to ask, or "
                                 f"drop the covered kind to assume"))
    return out


def ordered(nodes: dict[str, Node]) -> list[str]:
    """Node names in an order where every node comes after the nodes it needs (declaration
    order otherwise)."""
    out: list[str] = []
    seen: set[str] = set()

    def visit(nid: str) -> None:
        if nid in seen:
            return
        seen.add(nid)
        for other in nodes[nid].needs:
            visit(other)
        out.append(nid)

    for nid in nodes:
        visit(nid)
    return out


# --- sources, decisions, destinations -------------------------------------


class OptionConfig(Strict):
    id: str
    action: str
    label: str
    when: str | None = None
    destination: str | None = None
    set: dict[str, str] = {}   # fields the option fixes on its action, for example a task's tier
    batch: bool = False        # items recommended for this option are shown together, not one by one


# --- M5 decisions (TD §10) ---
QUESTION_KINDS = ("reconcile", "covered", "finding", "verify")


def _decision_duration(v: Any) -> Any:
    if v is None:
        return v
    from docket.text.durations import parse_duration
    try:
        parse_duration(v)
    except Exception:
        raise ValueError(f"not a duration: {v!r} (use 45, 45m, 1h, 1.5h or 1h30m)") from None
    return v


RECONCILE_OPTIONS = ("done", "partly", "not_done", "rescope", "to_agenda", "drop")
VERIFY_OPTIONS = ("close", "keep", "update")
COVERED_OPTIONS = ("covered", "keep", "drop")


class ReconcileConfig(Strict):
    """Reconcile questions (TD §10.4): owned blocks that ended inside ``lookback`` before the run
    instant. Option kinds are fixed; labels are templates. At ``carry_limit`` carries, ``not_done``
    gives way to ``rescope``."""
    carry_limit: int = Field(3, ge=1)
    lookback: str | int = "3d"
    labels: dict[str, str] = {}

    @field_validator("lookback")
    @classmethod
    def _duration(cls, v: Any) -> Any:
        return _decision_duration(v)

    def days_back(self) -> int:
        """Whole days before the run's date that ``lookback`` reaches, at least one. Planning and
        the calendar destination see owned blocks that far back (TD §12.3): an elapsed block
        of a task placed again is moved, never created a second time."""
        from docket.text.durations import parse_duration
        return max(1, -(-parse_duration(self.lookback) // 1440))

    @field_validator("labels")
    @classmethod
    def _known(cls, v: dict[str, str]) -> dict[str, str]:
        bad = sorted(set(v) - set(RECONCILE_OPTIONS))
        if bad:
            raise ValueError(f"unknown reconcile option(s) {bad}; options are {list(RECONCILE_OPTIONS)}")
        return v


class VerifyConfig(Strict):
    """Task verification (TD §10.5): open tasks whose references resolve closed, or whose last
    actor is the operator after the task's date field."""
    max_per_run: int = Field(10, ge=0)
    date_field: str = "created"
    closed_states: list[str] = ["closed", "resolved", "done", "merged", "cancelled", "canceled"]
    labels: dict[str, str] = {}

    @field_validator("labels")
    @classmethod
    def _known(cls, v: dict[str, str]) -> dict[str, str]:
        bad = sorted(set(v) - set(VERIFY_OPTIONS))
        if bad:
            raise ValueError(f"unknown verify option(s) {bad}; options are {list(VERIFY_OPTIONS)}")
        return v


class CoveredConfig(Strict):
    """Covered questions (TD §10.4): option kinds are fixed, labels are templates."""
    labels: dict[str, str] = {}

    @field_validator("labels")
    @classmethod
    def _known(cls, v: dict[str, str]) -> dict[str, str]:
        bad = sorted(set(v) - set(COVERED_OPTIONS))
        if bad:
            raise ValueError(f"unknown covered option(s) {bad}; options are {list(COVERED_OPTIONS)}")
        return v


class DecisionDefaults(Strict):
    """Derived option defaults (TD §10.2). Unset values fall back to the planning policy."""
    lead_days: int | None = Field(None, ge=0)
    est: str | int | None = None

    @field_validator("est")
    @classmethod
    def _duration(cls, v: Any) -> Any:
        return _decision_duration(v)


class Recommend(Strict):
    """Which option a question recommends when the assess task named none on offer (TD §10.2)."""
    default: str | None = None    # for any item
    surfaced: str | None = None   # for an item kept only because a rule said it must surface


class Decisions(Strict):
    option_sets: dict[str, dict[str, list[OptionConfig]]] = {}
    recommend: Recommend = Recommend()
    templates: dict[str, str] = {}
    reconcile: ReconcileConfig = ReconcileConfig()
    verify: VerifyConfig = VerifyConfig()
    covered: CoveredConfig = CoveredConfig()
    defaults: DecisionDefaults = DecisionDefaults()
    order: list[Literal["reconcile", "covered", "finding", "verify"]] = ["reconcile", "covered", "finding", "verify"]


class GrammarConfig(Strict):
    """The task-line grammar (TD §11.3); unset keys take ``docket.text.grammar.Grammar`` defaults."""
    open: str | None = None
    done: list[str] | None = None
    fields: Literal["bracket", "inline", "delimited"] | None = None
    separator: str | None = None
    assign: str | None = None


class ImperativeRule(Strict):
    deny: list[str] = []
    default_verb: str | None = None


class QualityConfig(Strict):
    """Task quality rules (TD §11.5). ``defaults`` fills a missing required field; ``due`` accepts ``+Nd``."""
    imperative: ImperativeRule | None = None
    max_length: int | None = None
    require: list[str] = []
    # Fields of ``require`` a task may lack: written in their place when the item has a value,
    # left out when it has none (a due date for a task that can wait).
    optional: list[str] = []
    defaults: dict[str, str] = {}
    evidence: Literal["required", "optional"] = "optional"
    fields: dict[str, str] = {}   # required attribute -> the field's name on the line ("" for a field with no key)
    duration: Literal["compact", "minutes"] = "compact"   # an estimate as 1h30m, or as 90m


class RedactConfig(Strict):
    """Redaction for a shared destination (TD §11.4)."""
    people: bool = False
    figures: list[str] = []
    classes: dict[str, str] = {}
    class_field: str | None = None
    entity: str | None = None
    person: str | None = None
    figure: str | None = None


class Destination(Strict):
    adapter: str
    description: str | None = None
    visibility: Literal["private", "shared"] = "private"
    path: str | None = None
    section: str | None = None
    line_template: str | None = None
    quality: QualityConfig | None = None
    marker: str | None = None
    policy: dict[str, Any] | None = None
    # --- M6 destinations ---
    grammar: GrammarConfig | None = None
    sections: dict[str, str] = {}
    carry_field: str | None = None
    # a task store: give an open line in its own sections that carries no key its key (TD §11.5)
    adopt: bool = False
    capacity: int | None = None
    new_note: str | None = None        # an agenda: the text of a forum's note that does not exist yet
    pattern: str | None = None
    calendar: str | None = None
    title_template: str | None = None
    notes_template: str | None = None
    program: list[str] | None = None
    env: list[str] = []
    timeout_s: float | None = None
    tools: dict[str, str] = {}
    redact: RedactConfig | None = None


# --- planning (TD §12) -------------------------------------------------------

ORDERING_KEYS = ("imminent", "overdue", "due", "tier", "size", "carry")
Duration = str | int


def _check_duration(v: Any) -> Any:
    if v is None:
        return v
    from docket.text.durations import parse_duration
    try:
        parse_duration(v)
    except Exception:
        raise ValueError(f"not a duration: {v!r} (use 45, 45m, 1h, 1.5h or 1h30m)") from None
    return v


def _check_range(v: Any) -> Any:
    if v is None:
        return v
    from docket.plan.types import parse_clock_range
    try:
        parse_clock_range(v)
    except ValueError:
        raise ValueError(f"not a time range: {v!r} (use HH:MM-HH:MM)") from None
    return v


class PlanningBatch(Strict):
    max_minutes: Duration = "15m"   # tasks at or under this batch together; 0 disables batching
    block_max: Duration = "1h"      # the longest a daily batch block grows


class PlanningPolicy(Strict):
    """Scheduling policy. Every value has a neutral default; durations use the duration parser."""
    workdays: list[Weekday] | None = None          # default: profile.workdays
    day_window: str = "09:00-17:00"
    deep_window: str | None = None
    deep_kinds: list[str] = ["deep"]              # block kinds that must sit inside deep_window
    deep_min: Duration = "60m"                     # a task this long or longer is a deep block
    # Preferences, not rules (TD §12.3). A slot inside ``prefer_window`` is taken first, on a
    # later day if the due date allows. ``daily_target`` is how much planned work a day should
    # hold: days fill to it in turn, and go over it only when no lighter day is on time.
    prefer_window: str | None = None
    daily_target: Duration | None = None
    # A task longer than this is split into even parts, one a day (TD §12.3).
    split_over: Duration | None = None
    lunch: str | None = None
    granularity: Duration = "15m"
    min_block: Duration = "30m"
    max_block: Duration = "2h"
    meeting_buffer: Duration = "0m"
    daily_cap: Duration | None = None
    horizon_days: int = Field(14, ge=1, le=366)
    move_cap: int = Field(3, ge=0)
    move_lock: Duration = "0m"
    ordering: list[Literal["imminent", "overdue", "due", "tier", "size", "carry"]] = \
        ["imminent", "overdue", "due", "tier", "size"]
    imminent_days: int = Field(2, ge=0)
    # Days from the run to the due date of a task that names none. None: such a task has no
    # due date; it is placed in the earliest slot that fits, after what is due, and is never late.
    default_lead_days: int | None = Field(3, ge=0)
    default_est: Duration = "30m"
    busy_kinds: list[CalendarKind] = ["meeting", "focus", "soft_hold", "out_of_office", "boundary", "mirrored",
                                      "owned"]
    window_available_kinds: list[CalendarKind] = ["soft_hold", "out_of_office"]
    buffer_kinds: list[CalendarKind] = ["meeting"]
    batch: PlanningBatch = PlanningBatch()

    @field_validator("granularity", "min_block", "max_block", "meeting_buffer", "daily_cap", "move_lock",
                     "default_est", "deep_min", "daily_target", "split_over")
    @classmethod
    def _durations(cls, v: Any) -> Any:
        return _check_duration(v)

    @field_validator("day_window", "deep_window", "lunch")
    @classmethod
    def _ranges(cls, v: Any) -> Any:
        return _check_range(v)


class PlanningCalendar(Strict):
    """Where availability is read from and which calendar blocks go to.

    ``provider: destination`` reads the calendar destination itself, so blocks a run applied
    are the owned blocks the next run keeps, moves or releases. ``json_file`` reads a JSON
    fixture at ``path``. Either way an event is owned when its notes carry the destination's
    ownership marker (``destinations.<destination>.marker``); there is no second scheme.
    """
    provider: Literal["json_file", "destination"] = "json_file"
    path: str | None = None             # json_file: the fixture file
    calendar: str = "work"              # calendar id blocks are written to
    destination: str = "calendar"       # destination id carried on calendar operations (and its marker)
    key_property: str = "docket_key"    # json_file: a fixture property that holds an ownership key

    @model_validator(mode="after")
    def _path_for_file(self) -> "PlanningCalendar":
        if self.provider == "json_file" and not self.path:
            raise ValueError("provider json_file needs path")
        return self


class PlanningTasks(Strict):
    destination: str | None = None      # default: the first markdown_tasks destination
    sections: list[str] | None = None   # headings to schedule from, in tier order; default all
    fields: dict[str, str] = {}         # task attribute -> inline field name (due, est, start, tier, when, ...)


class MirrorAs(Strict):
    title: str = "Busy"
    transparency: Literal["opaque", "transparent"] = "opaque"


class PlanningMirror(Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)
    from_: str = Field(alias="from")
    to: str
    as_: MirrorAs = Field(MirrorAs(), alias="as")
    respect_deletions: bool = True
    include_all_day: bool = False


class PlanningTitles(Strict):
    task: str = "{title}"
    batch: str = "{count} small tasks"
    notes: str = ""
    batch_notes: str = "{#members}- {title}\n{/members}"
    # The hosts whose links the note templates may show (``{url}``, ``{links}``). A shared
    # calendar is read by others and a URL can spell out a page's title. None: every web link.
    link_hosts: list[str] | None = None

    @field_validator("task", "batch", "notes", "batch_notes")
    @classmethod
    def _tpl(cls, v: str) -> str:
        from docket.text import templates
        templates.parse(v)
        return v


class PlanningStanding(Strict):
    """A block a workspace wants on the calendar every working day, such as its own planning
    session (TD §12.9). It is placed before tasks, in the earliest legal slot of its day: the
    first workday after the run (``next_workday``) or the run's own day (``today``)."""
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    title: str = Field(min_length=1)
    est: Duration
    on: Literal["next_workday", "today"] = "next_workday"
    notes: str = ""

    @field_validator("est")
    @classmethod
    def _est(cls, v: Any) -> Any:
        _check_duration(v)
        from docket.text.durations import parse_duration
        if parse_duration(v) <= 0:
            raise ValueError(f"a standing block needs a duration above zero, not {v!r}")
        return v


class Planning(Strict):
    policy: PlanningPolicy = PlanningPolicy()
    calendar: PlanningCalendar | None = None
    tasks: PlanningTasks = PlanningTasks()
    mirrors: list[PlanningMirror] = []
    titles: PlanningTitles = PlanningTitles()
    standing: list[PlanningStanding] = []
    key_prefix: str = "dk_"

    @field_validator("standing")
    @classmethod
    def _unique_standing(cls, v: list[PlanningStanding]) -> list[PlanningStanding]:
        ids = [s.id for s in v]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"standing block ids must be unique; repeated: {dup}")
        return v


# --- shapes (TD §14.2) ------------------------------------------------------


class AcceptedShape(Strict):
    """An unexpected shape the operator reviewed and accepted; reports stop listing it."""
    area: str
    detail: str
    reason: str


class ShapesConfig(Strict):
    accepted: list[AcceptedShape] = []


# --- top level -------------------------------------------------------------


class DocketConfig(Strict):
    version: Literal[1]
    includes: list[str] = []
    presets: list[str] = []
    graphs: dict[str, dict[str, Node]] = {}
    profile: Profile
    sources: dict[str, Source] = {}
    transports: dict[str, TransportConfig] = {}
    signals: dict[str, Any] = {}
    tasks: TasksConfig = TasksConfig()
    decisions: Decisions = Decisions()
    destinations: dict[str, Destination] = {}
    planning: Planning = Planning()
    journal: JournalConfig = JournalConfig()
    shapes: ShapesConfig = ShapesConfig()

    @model_validator(mode="after")
    def _graph_shapes(self) -> "DocketConfig":
        for gid, nodes in self.graphs.items():
            if not nodes:
                raise ValueError(f"graphs.{gid} has no nodes")
            problems = graph_problems(nodes)
            if problems:
                raise ValueError(f"graphs.{gid}: " + "; ".join(problems))
        return self


SECTIONS: tuple[str, ...] = tuple(k for k in DocketConfig.model_fields if k not in ("includes",))


# --- relationship sets (TD §3.2) -------------------------------------------


def _domain(handle: str) -> str | None:
    return handle.rsplit("@", 1)[1].lower() if "@" in handle else None


def relationship_sets(profile: Profile) -> dict[str, list[str]]:
    """Compute ``chain``, ``reports``, ``skip_reports``, ``peers``, ``group:<id>``,
    ``role:<name>`` and ``external`` from ``reports_to``, roles and mail domains."""
    by_id = {p.id: p for p in profile.people}
    chain: list[str] = []
    cur = profile.me.reports_to
    seen: set[str] = set()
    while cur and cur in by_id and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = by_id[cur].reports_to
    reports = sorted(p.id for p in profile.people if p.reports_to == "me")
    skip = sorted(p.id for p in profile.people if p.reports_to in reports)
    peers = sorted(
        p.id for p in profile.people
        if profile.me.reports_to and p.reports_to == profile.me.reports_to
    )
    sets: dict[str, list[str]] = {"chain": chain, "reports": reports, "skip_reports": skip, "peers": peers}
    for g in profile.groups:
        sets[f"group:{g.id}"] = sorted(g.members)
    roles: dict[str, list[str]] = {}
    for p in profile.people:
        for r in p.roles:
            roles.setdefault(f"role:{r}", []).append(p.id)
    sets.update({k: sorted(v) for k, v in roles.items()})
    internal = {d.lower() for d in profile.internal_domains}
    external: list[str] = []
    if internal:
        for p in profile.people:
            doms = {d for d in (_domain(h) for h in p.handles.values()) if d}
            if doms and not doms & internal:
                external.append(p.id)
    sets["external"] = sorted(external)
    return sets
