"""Configuration models (TD §3.2).

Every top-level section is a Pydantic model with ``extra="forbid"``. Sections
whose full content belongs to later milestones (signals, tasks, planning,
journal, hooks, shapes) are typed as open maps for now; the top-level
structure, ``profile``, ``workflows``, ``sources``, ``transports``, ``decisions``
and ``destinations`` are strict. ``sources``, ``transports`` and ``signals.units``
are modelled in ``docket.config.source_models``.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from docket.config.source_models import Source, TransportConfig  # noqa: F401  (sections owned there)

STAGES: tuple[str, ...] = ("collect", "assess", "merge", "review", "decide", "plan", "approve", "apply", "close")
StageName = Literal["collect", "assess", "merge", "review", "decide", "plan", "approve", "apply", "close"]
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


class ClassifyRule(Strict):
    match: dict[str, Any]
    kind: str


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


# --- top level -------------------------------------------------------------


class DocketConfig(Strict):
    version: Literal[1]
    includes: list[str] = []
    presets: list[str] = []
    workflows: dict[str, Workflow] = {}
    profile: Profile
    sources: dict[str, Source] = {}
    transports: dict[str, TransportConfig] = {}
    signals: dict[str, Any] = {}
    tasks: dict[str, Any] = {}
    decisions: Decisions = Decisions()
    destinations: dict[str, Destination] = {}
    planning: dict[str, Any] = {}
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
