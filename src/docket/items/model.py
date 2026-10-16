"""The item model of the merge artifact (TD §7, §8.4).

Upstream stages pass items as plain dicts; ``normalize`` validates a merged
item against ``Item`` and returns its JSON form, so the artifact's shape is
fixed here. Unknown keys (fields later stages add) pass through.

``from`` stays the sender's display string, which templates render; the
resolved sender (``from.handle``, ``from.person``, ``from.relation`` of TD §7.1)
is ``sender``.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Open(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Sender(Open):
    handle: str | None = None
    display: str | None = None
    person: str | None = None
    relation: str = "unknown"
    relations: list[str] = []
    is_me: bool = False
    domain: str | None = None


class Mentioned(Open):
    me: bool = False
    people: list[str] = []
    groups: list[str] = []
    topics: list[str] = []
    entities: list[str] = []


class Reference(Open):
    pattern: str
    value: str


class Signals(Open):
    importance: int = 0
    urgency: int = 0
    surface: bool = False
    surface_rules: list[str] = []
    dismiss: list[dict[str, Any]] = []
    dismissed: bool = False
    route: str | None = None
    area: str | None = None
    topic: str | None = None
    sensitivity: str | None = None
    deadline: dict[str, Any] | None = None
    tags: list[str] = []
    assess: Literal["agent", "rules_only"] = "agent"
    assess_rules: list[str] = []
    reasons: list[str] = []
    expect_in: list[dict[str, Any]] = []
    rules: list[str] = []


class Item(Open):
    id: str
    source: str
    unit: str = ""
    identity: str = ""
    record_id: str | None = None
    title: str = ""
    text: str = ""
    from_: str = Field("", alias="from")
    at: str = ""
    url: str = ""
    fields: dict[str, Any] = {}
    handle: str = ""
    thread: str = ""
    to: list[str] = []
    cc: list[str] = []
    mentions: list[str] = []
    sender: Sender = Sender()
    mentioned: Mentioned = Mentioned()
    positions: list[str] = []
    references: list[Reference] = []
    keys: list[str] = []
    topic: str = ""
    topic_basis: Literal["reference", "thread", "agent", "item"] = "item"
    signals: Signals = Signals()
    agent: dict[str, Any] | None = None
    agent_pending: bool = False
    components: dict[str, int] = {}
    score: int = 0
    keep: bool = False
    status: Literal["kept", "dropped", "suppressed", "tracked", "pending"] = "pending"
    reason: str | None = None
    sources: list[str] = []
    evidence: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    suppressed: dict[str, Any] | None = None
    tracked: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = []


def normalize(it: dict[str, Any]) -> dict[str, Any]:
    return Item.model_validate(it).model_dump(mode="json", by_alias=True)
