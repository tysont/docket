"""Builders for signal tests: an invented profile, items and a ``SignalEnv`` without a workspace."""
from __future__ import annotations

from typing import Any

from docket.config.models import Profile
from docket.signals.engine import SignalEnv, evaluate
from docket.signals.models import parse

NOW = "2026-10-15T18:00:00+00:00"

PROFILE = {
    "timezone": "UTC",
    "internal_domains": ["orbit.example"],
    "me": {"name": "Rae Holm", "aliases": ["Rae"], "handles": {"chat": "u/100", "mail": "rae@orbit.example"},
           "reports_to": "vic"},
    "people": [
        {"id": "vic", "name": "Vic Arden", "handles": {"chat": "u/200", "mail": "vic@orbit.example"}},
        {"id": "tam", "name": "Tam Ober", "handles": {"chat": "u/300"}, "reports_to": "me", "roles": ["report"]},
        {"id": "ivo", "name": "Ivo Penn", "handles": {"chat": "u/400"}, "reports_to": "tam"},
        {"id": "kai", "name": "Kai Lund", "handles": {"chat": "u/500"}, "reports_to": "vic"},
        {"id": "eve", "name": "Eve Stroud", "handles": {"mail": "eve@client-co.example"}},
    ],
    "groups": [{"id": "core", "name": "Core crew", "members": ["tam", "ivo"]}],
    "topics": [{"id": "relay", "aliases": ["relay service"], "area": "Platform", "owned": True},
               {"id": "billing", "aliases": ["invoices"], "area": "Finance", "owned": False}],
    "entities": [{"id": "zenith", "aliases": ["Zenith Ltd"], "protected": True, "strategic": True},
                 {"id": "minor", "aliases": ["Minor Co"]}],
}


def item(i: str, **kw: Any) -> dict[str, Any]:
    base = {"id": i, "source": "chat", "unit": "", "identity": i, "record_id": "r_" + i, "title": "", "text": "",
            "from": "", "handle": "", "at": "2026-10-15T12:00:00+00:00", "url": "", "fields": {}, "thread": "",
            "to": [], "cc": [], "mentions": []}
    base.update(kw)
    if not base["title"]:
        base["title"] = base["text"][:80]
    return base


def env(rules: list[dict[str, Any]] | None = None, *, references: dict[str, Any] | None = None,
        scoring: dict[str, Any] | None = None, budgets: dict[str, Any] | None = None,
        units: dict[tuple[str, str], dict[str, Any]] | None = None, kinds: dict[str, str] | None = None,
        profile: dict[str, Any] | None = None, now: str | None = NOW) -> SignalEnv:
    from docket.signals.engine import instant
    from datetime import date
    raw: dict[str, Any] = {"rules": rules or [], "references": references or {}}
    if scoring is not None:
        raw["scoring"] = scoring
    if budgets is not None:
        raw["budgets"] = budgets
    cfg = parse(raw)
    return SignalEnv(cfg, Profile.model_validate(profile or PROFILE), [f"test.yaml:{i + 1}" for i in range(len(
        cfg.rules))], units or {}, kinds or {"chat": "codemode", "mail": "mcp_stdio", "tickets": "files"},
        instant(now), date(2026, 10, 15))


def run(rules: list[dict[str, Any]], items: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return evaluate(items, env(rules, **kw))


def fired(rules: list[dict[str, Any]], it: dict[str, Any], **kw: Any) -> bool:
    return run(rules, [it], **kw)[0]["signals"]["rules"] == [r["id"] for r in rules]


def rule(when: dict[str, Any], then: dict[str, Any] | None = None, rid: str = "r") -> dict[str, Any]:
    return {"id": rid, "when": when, "then": then or {"tags": ["hit"]}}
