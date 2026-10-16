"""Deterministic task packs (TD §9.2).

A pack holds everything its task needs and nothing else: each item's fields
and evidence lines with line ids, the context of the units the items came
from, the profile excerpts the items touch (people named, topics matched) and
the vocabulary. Items sort must-surface first, then by score descending, then
by id, and batch to the kind's ``max_items`` and ``max_bytes``. A must-surface
item is never dropped or trimmed: when it alone exceeds the byte budget, its
pack exceeds the budget and records the unexpected shape ``pack.over_budget``.
Another item that alone exceeds the budget keeps its first lines and is marked
``truncated``.

The pack's digest is the task input digest. The task id derives from it, so an
unchanged pack reuses its accepted result (TD §9.6). Items are read
defensively: a field the item model does not carry renders empty.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

from docket.config.digest import canonical_json, digest
from docket.shapes.unexpected import unexpected

if TYPE_CHECKING:
    from docket.config.models import DocketConfig
    from docket.tasks.kinds import TaskKind

ITEM_FIELDS = ("id", "source", "unit", "title", "from", "at", "url")
PROPOSAL_FIELDS = ("kind", "owner", "ask", "evidence", "deadline", "topic")


# --- the pack model (``docket schema <kind>-input``) -------------------------


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, serialize_by_alias=True)


class PackLine(_Open):
    line: str = Field(description="The line id evidence quotes name, such as L1.")
    text: str


class PackItem(_Open):
    id: str
    source: str
    unit: str = ""
    title: str = ""
    from_: Any = Field(None, alias="from")
    at: str = ""
    url: str = ""
    fields: dict[str, Any] = {}
    lines: list[PackLine] = Field(description="The item's evidence lines; quotes are copied from these.")
    must_surface: bool = False
    truncated: bool = False
    proposal: Optional[dict[str, Any]] = Field(None, description="review: the assessment's proposal, without "
                                                                   "its score.")


class PackUnit(_Open):
    source: str
    id: str
    name: Optional[str] = None
    kind: Optional[str] = None
    class_: Optional[str] = Field(None, alias="class")
    tier: Optional[str] = None


class PackPerson(_Open):
    id: str
    name: str
    aliases: list[str] = []
    roles: list[str] = []
    relations: list[str] = []


class PackTopic(_Open):
    id: str
    aliases: list[str] = []
    area: Optional[str] = None
    owned: bool = False


class PackMe(_Open):
    name: str
    aliases: list[str] = []


class PackProfile(_Open):
    me: PackMe
    people: list[PackPerson] = []
    topics: list[PackTopic] = []


class PackBudget(_Open):
    max_items: int
    max_bytes: int
    bytes: int
    over_budget: bool = False


class Pack(_Open):
    """A task pack: the whole input of one task (TD §9.2)."""
    task_id: str
    input_digest: str
    kind: str
    stage: str
    spec: str = Field(description="Digest of the kind's instructions and output model.")
    items: list[PackItem]
    units: list[PackUnit] = []
    profile: PackProfile
    vocabulary: dict[str, list[str]] = {}
    budget: PackBudget


def pack_model(kind: str) -> type[BaseModel]:
    name = "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", kind) if p) + "Input"
    return create_model(name, __base__=Pack, __doc__=f"The pack of a {kind} task (TD §9.2).",
                        kind=(Literal[kind], ...))  # type: ignore[valid-type]


# --- context -----------------------------------------------------------------


def _terms_regex(terms: list[str]) -> re.Pattern[str] | None:
    terms = sorted({t.strip() for t in terms if isinstance(t, str) and len(t.strip()) >= 2}, key=len, reverse=True)
    if not terms:
        return None
    parts = []
    for t in terms:
        left = r"(?<![\w])" if t[0].isalnum() else ""
        right = r"(?![\w])" if t[-1].isalnum() else ""
        parts.append(left + re.escape(t) + right)
    return re.compile("|".join(parts), re.I)


@dataclass
class PackContext:
    """What every pack of one evaluation shares: profile matchers, unit context, vocabulary."""
    me: dict[str, Any]
    people: list[tuple[dict[str, Any], re.Pattern[str] | None]] = field(default_factory=list)
    topics: list[tuple[dict[str, Any], re.Pattern[str] | None]] = field(default_factory=list)
    units: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def build(cls, config: "DocketConfig", units: dict[tuple[str, str], dict[str, Any]] | None = None) -> "PackContext":
        from docket.config.models import relationship_sets
        prof = config.profile
        rel = relationship_sets(prof)
        relations: dict[str, list[str]] = {}
        for name, members in sorted(rel.items()):
            for m in members:
                relations.setdefault(m, []).append(name)
        people = []
        for p in prof.people:
            row = {"id": p.id, "name": p.name, "aliases": list(p.aliases), "roles": list(p.roles),
                   "relations": relations.get(p.id, [])}
            people.append((row, _terms_regex([p.name, *p.aliases, *p.handles.values()])))
        topics = []
        for t in prof.topics:
            row = {"id": t.id, "aliases": list(t.aliases), "area": t.area, "owned": t.owned}
            topics.append((row, _terms_regex([t.id, *t.aliases])))
        return cls(me={"name": prof.me.name, "aliases": list(prof.me.aliases)}, people=people, topics=topics,
                   units=units or {})


def load_units(db: Any, sources: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Unit context from the unit registry (TD §6.9) for the given sources."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not sources:
        return out
    try:
        q = ",".join("?" for _ in sources)
        rows = db.execute(f"SELECT source, unit_id, name, kind, class, tier FROM units WHERE source IN ({q})",
                          sorted(sources)).fetchall()
    except Exception:  # the registry is context only; a pack without it is still complete
        return out
    for r in rows:
        out[(r[0], r[1])] = {"source": r[0], "id": r[1], "name": r[2], "kind": r[3], "class": r[4],
                             "tier": None if r[5] is None else str(r[5])}
    return out


# --- item views ----------------------------------------------------------------


def _scalar(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _plain(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return _scalar(v)


def item_lines(it: dict[str, Any]) -> list[dict[str, str]]:
    """The item's evidence lines with ids L1, L2, ... (from ``evidence`` when mapped, else ``text``)."""
    src = it.get("evidence")
    text = ""
    if isinstance(src, list):
        # Merge replaces ``evidence`` with provenance dicts; only scalar entries are quotable text.
        text = "\n".join(str(x) for x in src if isinstance(x, (str, int, float)))
    elif isinstance(src, str):
        text = src
    if not text.strip():
        text = "" if it.get("text") is None else str(it.get("text"))
    if not text.strip():
        text = str(it.get("title") or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return [{"line": f"L{i}", "text": ln} for i, ln in enumerate(lines, 1)]


def must_surface(it: dict[str, Any]) -> bool:
    return bool(it.get("must_surface")) or it.get("surface") == "always"


def score_of(it: dict[str, Any]) -> int:
    try:
        return int(it.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def item_view(kind: "TaskKind", it: dict[str, Any]) -> dict[str, Any]:
    v: dict[str, Any] = {k: ("" if it.get(k) is None else _scalar(it.get(k))) for k in ITEM_FIELDS}
    if isinstance(it.get("from"), dict):
        v["from"] = _plain(it["from"])
    fields = it.get("fields")
    v["fields"] = _plain(fields) if isinstance(fields, dict) else {}
    v["lines"] = item_lines(it)
    v["must_surface"] = must_surface(it)
    v["truncated"] = False
    if kind.view == "review":
        a = it.get("assessment") if isinstance(it.get("assessment"), dict) else {}
        v["proposal"] = {k: _plain(a.get(k)) for k in PROPOSAL_FIELDS if a.get(k) not in (None, "", [])}
    return v


def _size(v: Any) -> int:
    return len(canonical_json(v))


def _trim(view: dict[str, Any], budget: int) -> dict[str, Any]:
    v = dict(view)
    lines = list(v["lines"])
    while lines and _size({**v, "lines": lines, "truncated": True}) > budget:
        lines.pop()
    v["lines"], v["truncated"] = lines, True
    return v


def _excerpts(ctx: PackContext, views: list[dict[str, Any]]) -> dict[str, Any]:
    hay = "\n".join(
        " ".join([str(v.get("title") or ""), str(v.get("from") or ""), *(ln["text"] for ln in v["lines"])])
        for v in views)
    people = [row for row, rx in ctx.people if rx is not None and rx.search(hay)]
    topics = [row for row, rx in ctx.topics if rx is not None and rx.search(hay)]
    return {"me": ctx.me, "people": people, "topics": topics}


def _vocabulary(kind: "TaskKind", ctx: PackContext) -> dict[str, list[str]]:
    voc: dict[str, list[str]] = {}
    for f in kind.fields:
        if f.type == "enum":
            voc[f.name] = f.value_names()
        for sub in f.rows:
            if sub.type == "enum":
                voc[f"{f.name}.{sub.name}"] = sub.value_names()
    voc["topics"] = sorted(row["id"] for row, _ in ctx.topics)
    return voc


def build_packs(kind: "TaskKind", items: list[dict[str, Any]], ctx: PackContext) -> list[dict[str, Any]]:
    """Packs for ``items`` (already selected for the kind), in a deterministic order."""
    rows = sorted(items, key=lambda it: (not must_surface(it), -score_of(it), str(it.get("id"))))
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_bytes = 0
    for it in rows:
        view = item_view(kind, it)
        n = _size(view)
        if n > kind.max_bytes and not view["must_surface"]:
            view = _trim(view, kind.max_bytes)
            n = _size(view)
        if cur and (len(cur) >= kind.max_items or cur_bytes + n > kind.max_bytes):
            groups.append(cur)
            cur, cur_bytes = [], 0
        cur.append(view)
        cur_bytes += n
    if cur:
        groups.append(cur)
    packs = []
    voc = _vocabulary(kind, ctx)
    for views in groups:
        used = sorted({(v["source"], v["unit"]) for v in views})
        units = [ctx.units[k] for k in used if k in ctx.units]
        total = sum(_size(v) for v in views)
        body = {
            "kind": kind.name, "stage": kind.stage, "spec": kind.spec_digest, "items": views, "units": units,
            "profile": _excerpts(ctx, views), "vocabulary": voc,
            "budget": {"max_items": kind.max_items, "max_bytes": kind.max_bytes, "bytes": total,
                       "over_budget": total > kind.max_bytes},
        }
        dg = digest(body)
        if total > kind.max_bytes:
            unexpected("pack.over_budget", f"{kind.name} must-surface items exceed max_bytes",
                       sample={"bytes": total, "max_bytes": kind.max_bytes, "items": len(views)},
                       reading="issue the pack over its byte budget; no must-surface item is dropped",
                       extend=f"tasks.{'' if kind.core else 'kinds.'}{kind.name}.max_bytes")
        packs.append({"task_id": "t_" + dg[:12], "input_digest": dg, **body})
    return packs


def pack_ids(pack: dict[str, Any]) -> list[str]:
    return [it["id"] for it in pack["items"]]


def carry_key(kind: "TaskKind", view: dict[str, Any]) -> str:
    """Per-item carry-forward key (TD §9.6): the item's evidence and assessment as the task saw them."""
    return digest({"kind": kind.name, "spec": kind.spec_digest, "item": view})
