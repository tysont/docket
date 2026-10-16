"""Configuration models for ``sources``, ``transports`` and ``signals.units`` (TD §6).

A source binds a transport instance, a cursor model, a request plan, a paging
contract, a response contract and a record-to-item mapping. Presets supply the
plan, the contracts and the default mapping; the workspace supplies the
instance, its parameters, auth commands and budgets (TD §6.1).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TransportKind = Literal["exec_jsonl", "http_json", "mcp_stdio", "mcp_http", "codemode", "files", "replay"]
CursorModel = Literal["snapshot", "incremental", "units", "id-scan", "cadence"]
Capability = Literal["read", "discover", "resolve", "context"]
FieldType = Literal["str", "int", "float", "number", "bool", "timestamp", "list", "dict", "any"]
EndSignal = Literal["token_absent", "flag", "short_page", "empty_page", "past_interval"]


# Every key a source's ``mapping`` may name, with what it maps (TD §7.1, docket.items.mapping).
# ``fields.<name>`` keys are free-form and read by rules as ``field: { name: ... }``.
MAPPING_KEY_DOCS: dict[str, str] = {
    "id": "The record's id; item identity derives from it.",
    "title": "The item's one-line title.",
    "text": "The item's body text.",
    "from": "The sender as templates render it (a name or an address).",
    "at": "The record's timestamp (ISO 8601).",
    "url": "A link back to the record.",
    "unit": "The unit the record belongs to (a space, channel, queue or project).",
    "from.handle": "The sender's handle, resolved through profile handles (default: from).",
    "from.display": "The sender's display name.",
    "thread": "The thread key; records sharing one merge (default: the item id).",
    "to": "Handles addressed (a string or a list).",
    "cc": "Handles copied (a string or a list).",
    "mentions": "Handles mentioned (a string or a list).",
}
FIELDS_KEY, FIELDS_PATTERN = "fields.<name>", r"^fields\..+$"
FIELDS_DOC = "A custom field; rules read it as field: { name: <name> }."
# Words people reach for, mapped to the key that holds them (an unknown key's suggestion).
MAPPING_SYNONYMS = {
    "body": "text", "content": "text", "description": "text", "message": "text", "snippet": "text",
    "summary": "title", "subject": "title", "name": "title", "headline": "title",
    "author": "from", "sender": "from", "user": "from", "requester": "from", "email": "from",
    "date": "at", "time": "at", "timestamp": "at", "created": "at", "created_at": "at", "updated": "at",
    "updated_at": "at", "ts": "at", "link": "url", "href": "url", "permalink": "url", "key": "id",
    "channel": "unit", "space": "unit", "queue": "unit", "project": "unit", "handle": "from.handle",
    "display": "from.display", "display_name": "from.display", "recipients": "to", "mention": "mentions",
}


def mapping_key_suggestion(key: str) -> str | None:
    """The valid mapping key closest to an unknown one: a custom-field typo, a synonym, then spelling."""
    import difflib
    k = str(key)
    low = k.lower()
    for prefix in ("field.", "fields_", "field_", "fields:", "field:"):
        if low.startswith(prefix) and len(k) > len(prefix):
            return "fields." + k[len(prefix):]
    if low in MAPPING_SYNONYMS:
        return MAPPING_SYNONYMS[low]
    m = difflib.get_close_matches(low, list(MAPPING_KEY_DOCS), n=1, cutoff=0.6)
    return m[0] if m else None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- transports ---------------------------------------------------------------


class CredentialRef(Strict):
    """Where a credential lives. A value never appears in configuration (TD §16)."""
    env: str | None = None
    file: str | None = None


class RecordConfig(Strict):
    """Wrap the transport with a recorder writing request/response pairs (TP §7.1)."""
    path: str
    redact: list[str] = []  # response JSON paths ("$.a.b") replaced before writing


class TransportConfig(Strict):
    kind: TransportKind
    description: str | None = None
    command: list[str] | None = None          # exec_jsonl, mcp_stdio, codemode over stdio
    cwd: str | None = None
    env: dict[str, str] = {}                  # non-secret variables for the child
    env_from: dict[str, CredentialRef] = {}   # child variables read from credential sources
    url: str | None = None                    # http_json base URL, mcp_http or codemode endpoint
    headers: dict[str, str] = {}
    headers_from: dict[str, CredentialRef] = {}
    bearer: CredentialRef | None = None
    trust_env: bool = False                   # http: honor proxy variables of the environment
    tool: str | None = None                   # codemode: the code-execution tool
    code_arg: str = "code"                    # codemode: the tool argument that carries the script
    timeout_s: float = 30.0
    startup_timeout_s: float = 15.0
    max_response_bytes: int | None = None     # a larger body is a truncation signal
    concurrency: int = 4
    root: str | None = None                   # files: base directory (default: the workspace)
    path: str | None = None                   # replay: the recordings file
    record: RecordConfig | None = None


# --- sources --------------------------------------------------------------------


class CursorConfig(Strict):
    model: CursorModel = "snapshot"
    inner: Literal["snapshot", "incremental", "units", "id-scan"] | None = None  # cadence only
    weekdays: list[Weekday] | None = None   # cadence: read only on these days
    every_days: int | None = None           # cadence: read at most every N days
    lookback: str | None = None             # first read starts this far before the run instant
    position: Literal["order_key", "token"] = "order_key"
    unit_kind: str | None = None

    @property
    def effective(self) -> str:
        return (self.inner or "incremental") if self.model == "cadence" else self.model


class RequestTemplate(Strict):
    """One request, rendered against the read context. Which keys apply depends on
    the transport kind: ``op``+``args`` (exec), ``tool``+``args`` (MCP), ``script``+``args``
    (codemode), ``method``/``url``/``params``/``headers``/``body`` (HTTP), ``path``+``args`` (files)."""
    op: str | None = None
    tool: str | None = None
    script: str | None = None
    method: str | None = None
    url: str | None = None
    params: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body: Any = None
    path: str | None = None
    args: dict[str, Any] = {}

    def payload(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(mode="json").items() if v not in (None, {}, [])}


class RequestPlan(Strict):
    read: RequestTemplate | None = None
    discover: RequestTemplate | None = None
    ids: RequestTemplate | None = None      # id-scan: list ids
    get: RequestTemplate | None = None      # id-scan: read one id
    resolve: RequestTemplate | None = None


class PagingConfig(Strict):
    """Where a response keeps its entries, its continuation and its end-of-data signal."""
    kind: str = "entry"                     # the contract kind entries are checked against
    page_size: int = 100
    min_page_size: int = 10
    entries: str = "$.items"
    next: str | None = "$.next"
    end: str | None = None                  # path of an end flag (end_when: flag)
    end_when: list[EndSignal] = ["token_absent"]
    truncated: str | None = None            # path of a truncation marker
    too_large: list[str] = []               # regexes over raw error messages meaning "page too large"
    order_key: str | None = None            # per-entry ordering key (incremental position)


class DiscoverConfig(Strict):
    kind: str = "unit"
    entries: str = "$.units"
    next: str | None = None                 # continuation of a paged discovery
    id: str = "$.id"
    name: str | None = "$.name"
    type: str | None = "$.type"
    members: str | None = None


class VariationRule(Strict):
    reading: str
    use: str | None = None                  # read this path instead
    value: Any = None                       # substitute this value
    record: bool = True
    extend: str | None = None


class FieldVariations(Strict):
    missing: VariationRule | None = None
    unknown: VariationRule | None = None
    type: VariationRule | None = None


class ContractKind(Strict):
    required: dict[str, FieldType] = {}
    optional: dict[str, FieldType] = {}
    enums: dict[str, list[Any]] = {}
    unknown_fields: Literal["ignore", "record"] = "ignore"
    variations: dict[str, FieldVariations] = {}
    tombstone: str | None = None            # path whose truthy value marks a deleted record


class AuthConfig(Strict):
    check: list[str] | None = None
    refresh: list[str] | None = None
    timeout_s: float = 10.0
    instructions: str | None = None


class BudgetConfig(Strict):
    max_pages: int | None = None
    max_seconds: float | None = None
    concurrency: int = 4
    page_size: int | None = None
    min_page_size: int | None = None


class ResolveConfig(Strict):
    patterns: list[str] = []
    fields: dict[str, str] = {}             # state, last_activity, last_actor, title -> paths


class Source(Strict):
    kind: str | None = None                 # derived from the transport; ``files`` with ``path`` needs none
    description: str | None = None
    path: str | None = None                 # files shorthand
    extends: str | None = None              # a source preset id, for example mcp/chat-codemode
    transport: str | None = None            # a key of the top-level ``transports``
    params: dict[str, Any] = {}             # values the preset's request plan names as {params.x}
    capabilities: list[Capability] | None = None
    target: Literal["item", "context"] = "item"
    cursor: CursorConfig = CursorConfig()
    request: RequestPlan | None = None
    paging: PagingConfig = PagingConfig()
    discover: DiscoverConfig | None = None
    contract: dict[str, ContractKind] = {}
    auth: AuthConfig | None = None
    budget: BudgetConfig = BudgetConfig()
    resolve: ResolveConfig | None = None
    volatile: bool = False
    required: bool = False
    mapping: dict[str, str] = Field(default_factory=dict, json_schema_extra={
        "description": "Item field -> '$.path' in the record. Keys: " + ", ".join(MAPPING_KEY_DOCS)
                       + ", and fields.<name> for custom fields.",
        "propertyNames": {"anyOf": [{"enum": list(MAPPING_KEY_DOCS)}, {"pattern": FIELDS_PATTERN}]},
    })


# --- signals.units (TD §6.9) -----------------------------------------------------


class UnitMatch(Strict):
    source: str | None = None
    name_regex: str | None = None
    id_regex: str | None = None
    type: list[str] | None = None
    members_min: int | None = None
    members_max: int | None = None


class UnitRule(Strict):
    """When ``match`` holds, ``set`` assigns any of ``kind``, ``class`` and ``tier`` not
    already assigned by an earlier rule."""
    id: str | None = None
    match: UnitMatch = UnitMatch()
    set: dict[Literal["kind", "class", "tier"], str | int] = {}


class UnitSignals(Strict):
    rules: list[UnitRule] = []
