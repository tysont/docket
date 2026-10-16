"""Configuration models (TD §3.2).

Every top-level section is a Pydantic model with ``extra="forbid"``. Sections
whose full content belongs to later milestones (signals, tasks, journal,
hooks, transports, shapes) are typed as open maps for now; the top-level
structure, ``profile``, ``workflows``, ``sources``, ``decisions``,
``destinations`` and ``planning`` are strict.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

STAGES: tuple[str, ...] = ("collect", "assess", "merge", "review", "decide", "plan", "approve", "apply", "close")
StageName = Literal["collect", "assess", "merge", "review", "decide", "plan", "approve", "apply", "close"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SOURCE_KINDS: dict[str, frozenset[str]] = {
    # kind -> capabilities. Only ``files`` is implemented in M1; the others are
    # declared so configurations validate, and collect skips them with a record.
    "files": frozenset({"read"}),
    "mcp_stdio": frozenset({"read", "discover", "resolve", "context"}),
    "mcp_http": frozenset({"read", "discover", "resolve", "context"}),
    "codemode": frozenset({"read", "discover", "resolve", "context"}),
    "exec_jsonl": frozenset({"read", "discover", "resolve", "context"}),
    "http_json": frozenset({"read", "resolve", "context"}),
}
IMPLEMENTED_SOURCE_KINDS = frozenset({"files"})

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
IMPLEMENTED_ADAPTERS = frozenset({"markdown_tasks"})

ACTIONS = ("task.add", "agenda.add", "calendar.place", "message.draft", "now", "done", "watch", "dismiss")
ACTION_NEEDS_DESTINATION = {"task.add", "agenda.add", "calendar.place", "message.draft"}
WHEN_GUARDS = ("has_forum", "has_reply_channel", "has_deadline", "has_calendar", "source_is", "relation_is")
MAPPING_KEYS = ("id", "title", "text", "from", "at", "url", "unit")


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


class Entity(Strict):
    id: str
    aliases: list[str] = []
    protected: bool = False
    strategic: bool = False


class Forum(Strict):
    id: str
    name: str | None = None
    kind: str = "group"
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


# --- workflows -------------------------------------------------------------


class WorkflowReview(Strict):
    pool: int | None = None


class WorkflowQuestions(Strict):
    kinds: list[str] = ["finding"]
    bursts: int = 1
    budget: int | None = None


class Workflow(Strict):
    description: str | None = None
    stages: list[StageName]
    sources: list[str] | None = None
    pause_before: StageName | None = None
    refresh_on_resume: list[str] = []
    review: WorkflowReview | None = None
    questions: WorkflowQuestions = WorkflowQuestions()
    options: str | None = None
    journal: str | None = None
    hooks: dict[str, list[str]] = {}


# --- sources, decisions, destinations -------------------------------------


class Source(Strict):
    kind: str
    description: str | None = None
    path: str | None = None
    extends: str | None = None
    transport: dict[str, Any] | None = None
    cursor: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    contract: dict[str, Any] | None = None
    volatile: bool = False
    required: bool = False
    mapping: dict[str, str] = {}


class OptionConfig(Strict):
    id: str
    action: str
    label: str
    when: str | None = None
    destination: str | None = None


class Decisions(Strict):
    option_sets: dict[str, dict[str, list[OptionConfig]]] = {}
    templates: dict[str, str] = {}
    reconcile: dict[str, Any] = {}
    verify: dict[str, Any] = {}


class Destination(Strict):
    adapter: str
    description: str | None = None
    visibility: Literal["private", "shared"] = "private"
    path: str | None = None
    section: str | None = None
    line_template: str | None = None
    quality: dict[str, Any] | None = None
    marker: str | None = None
    policy: dict[str, Any] | None = None


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
    default_lead_days: int = Field(3, ge=0)
    default_est: Duration = "30m"
    busy_kinds: list[CalendarKind] = ["meeting", "focus", "soft_hold", "out_of_office", "boundary", "mirrored",
                                      "owned"]
    window_available_kinds: list[CalendarKind] = ["soft_hold", "out_of_office"]
    buffer_kinds: list[CalendarKind] = ["meeting"]
    batch: PlanningBatch = PlanningBatch()

    @field_validator("granularity", "min_block", "max_block", "meeting_buffer", "daily_cap", "move_lock",
                     "default_est", "deep_min")
    @classmethod
    def _durations(cls, v: Any) -> Any:
        return _check_duration(v)

    @field_validator("day_window", "deep_window", "lunch")
    @classmethod
    def _ranges(cls, v: Any) -> Any:
        return _check_range(v)


class PlanningCalendar(Strict):
    """Where availability is read from and which calendar blocks go to."""
    provider: Literal["json_file"] = "json_file"
    path: str
    calendar: str = "work"              # calendar id blocks are written to
    destination: str = "calendar"       # destination id carried on calendar operations
    key_property: str = "docket_key"    # event property holding the ownership key
    marker_regex: str | None = None     # or: a regex over notes whose group 1 is the key


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

    @field_validator("task", "batch", "notes", "batch_notes")
    @classmethod
    def _tpl(cls, v: str) -> str:
        from docket.text import templates
        templates.parse(v)
        return v


class Planning(Strict):
    policy: PlanningPolicy = PlanningPolicy()
    calendar: PlanningCalendar | None = None
    workflows: list[str] | None = None   # workflows that schedule; default every workflow with a plan stage
    tasks: PlanningTasks = PlanningTasks()
    mirrors: list[PlanningMirror] = []
    titles: PlanningTitles = PlanningTitles()
    key_prefix: str = "dk_"


# --- top level -------------------------------------------------------------


class DocketConfig(Strict):
    version: Literal[1]
    includes: list[str] = []
    presets: list[str] = []
    workflows: dict[str, Workflow] = {}
    profile: Profile
    sources: dict[str, Source] = {}
    transports: dict[str, Any] = {}
    signals: dict[str, Any] = {}
    tasks: dict[str, Any] = {}
    decisions: Decisions = Decisions()
    destinations: dict[str, Destination] = {}
    planning: Planning = Planning()
    journal: dict[str, Any] = {}
    hooks: dict[str, Any] = {}
    shapes: dict[str, Any] = Field(default_factory=dict)


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
