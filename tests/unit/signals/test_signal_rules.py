"""The rule language (TD §8.1, FR-SIG-02, FR-SIG-03): every predicate and effect, ordering,
surface-versus-dismiss and plugin predicates."""
from __future__ import annotations

import pytest

from docket.signals import plugins
from docket.signals.engine import needs_agent
from tests.signal_helpers import fired, item, rule, run

DM = ("chat", "spaces/dm")
ROOM = ("chat", "spaces/room")
UNITS = {DM: {"name": "Direct", "kind": "direct", "class": "direct", "tier": "1"},
         ROOM: {"name": "Deploy bots", "kind": "team", "class": "bots", "tier": "3"}}

# (id, when, item kwargs, expected)
PREDICATES = [
    ("source-id", {"source": "chat"}, {}, True),
    ("source-id-miss", {"source": ["mail", "tickets"]}, {}, False),
    ("source-kind", {"source": {"kind": "codemode"}}, {}, True),
    ("source-kind-miss", {"source": {"kind": "files"}}, {}, False),
    ("unit-kind", {"unit": {"kind": "direct"}}, {"unit": "spaces/dm"}, True),
    ("unit-class", {"unit": {"class": ["bots", "alerts"]}}, {"unit": "spaces/room"}, True),
    ("unit-class-miss", {"unit": {"class": "bots"}}, {"unit": "spaces/dm"}, False),
    ("unit-tier", {"unit": {"tier": 1}}, {"unit": "spaces/dm"}, True),
    ("unit-tier-max", {"unit": {"tier_max": 2}}, {"unit": "spaces/room"}, False),
    ("unit-id", {"unit": {"id": "spaces/room"}}, {"unit": "spaces/room"}, True),
    ("unit-id-regex", {"unit": {"id_regex": "dm$"}}, {"unit": "spaces/dm"}, True),
    ("unit-name-regex", {"unit": {"name_regex": "(?i)bots"}}, {"unit": "spaces/room"}, True),
    ("unit-unknown-unit", {"unit": {"kind": "direct"}}, {"unit": "spaces/other"}, False),
    ("from-relation-chain", {"from": {"relation": "chain"}}, {"handle": "u/200"}, True),
    ("from-relation-reports", {"from": {"relation": "reports"}}, {"handle": "u/300"}, True),
    ("from-relation-skip", {"from": {"relation": "skip_reports"}}, {"handle": "u/400"}, True),
    ("from-relation-peers", {"from": {"relation": "peers"}}, {"handle": "u/500"}, True),
    ("from-relation-external", {"from": {"relation": "external"}}, {"handle": "eve@client-co.example"}, True),
    ("from-relation-unknown", {"from": {"relation": "unknown"}}, {"handle": "u/999"}, True),
    ("from-relation-group", {"from": {"relation": "group:core"}}, {"handle": "u/400"}, True),
    ("from-relation-role", {"from": {"relation": "role:report"}}, {"handle": "u/300"}, True),
    ("from-relation-miss", {"from": {"relation": "chain"}}, {"handle": "u/300"}, False),
    ("from-person", {"from": {"person": ["tam", "kai"]}}, {"handle": "u/500"}, True),
    ("from-group", {"from": {"group": "core"}}, {"handle": "u/300"}, True),
    ("from-group-miss", {"from": {"group": "core"}}, {"handle": "u/500"}, False),
    ("from-domain", {"from": {"domain": "client-co.example"}}, {"handle": "eve@client-co.example"}, True),
    ("from-domain-sub", {"from": {"domain": "client-co.example"}}, {"handle": "a@mail.client-co.example"}, True),
    ("from-handle-regex", {"from": {"handle": "(?i)^no-?reply@"}}, {"handle": "noreply@tool.example"}, True),
    ("from-is-me", {"from": {"is_me": True}}, {"handle": "u/100"}, True),
    ("from-display-name", {"from": {"person": "vic"}}, {"from": "Vic Arden"}, True),
    ("to-direct-unit", {"to": "direct"}, {"unit": "spaces/dm"}, True),
    ("to-direct-addressed", {"to": "direct"}, {"to": ["rae@orbit.example"]}, True),
    ("to-cc", {"to": ["cc"]}, {"cc": ["RAE@orbit.example"]}, True),
    ("to-mentioned", {"to": "mentioned"}, {"text": "Rae, can you check?"}, True),
    ("to-mentioned-handle", {"to": "mentioned"}, {"mentions": ["u/100"]}, True),
    ("to-member", {"to": "member"}, {"text": "general news"}, True),
    ("to-member-miss", {"to": "member"}, {"to": ["rae@orbit.example"]}, False),
    ("mentions-me", {"mentions": {"me": True}}, {"text": "ping Rae Holm"}, True),
    ("mentions-me-alias-word", {"mentions": {"me": True}}, {"text": "the raelity is"}, False),
    ("mentions-people", {"mentions": {"people": ["tam"]}}, {"text": "Ask Tam Ober"}, True),
    ("mentions-people-relation", {"mentions": {"people": {"relation": "reports"}}}, {"text": "Tam Ober said"}, True),
    ("mentions-groups", {"mentions": {"groups": "core"}}, {"text": "the Core crew agreed"}, True),
    ("mentions-topics-owned", {"mentions": {"topics": {"owned": True}}}, {"text": "the relay service is slow"}, True),
    ("mentions-topics-not-owned", {"mentions": {"topics": {"owned": True}}}, {"text": "invoices are late"}, False),
    ("mentions-topics-id", {"mentions": {"topics": ["billing"]}}, {"text": "invoices are late"}, True),
    ("mentions-topics-area", {"mentions": {"topics": {"area": "Platform"}}}, {"text": "relay is down"}, True),
    ("mentions-entities-protected", {"mentions": {"entities": {"protected": True}}}, {"text": "Zenith Ltd asked"}, True),
    ("mentions-entities-unprotected", {"mentions": {"entities": {"protected": True}}}, {"text": "Minor Co asked"}, False),
    ("mentions-entities-strategic", {"mentions": {"entities": {"strategic": True}}}, {"text": "zenith again"}, True),
    ("text-regex", {"text": "(?i)\\boutage\\b"}, {"text": "An OUTAGE now"}, True),
    ("text-regex-case-sensitive", {"text": {"regex": "outage", "case": "sensitive"}}, {"text": "An OUTAGE"}, False),
    ("text-phrases", {"text": {"phrases": ["lunch order"]}}, {"text": "Lunch order closes"}, True),
    ("text-fields-title-only", {"text": {"regex": "body", "fields": ["title"]}}, {"title": "head", "text": "body"},
     False),
    ("field-equals", {"field": {"name": "priority", "equals": "P1"}}, {"fields": {"priority": "p1"}}, True),
    ("field-in", {"field": {"name": "priority", "in": ["P1", "P2"]}}, {"fields": {"priority": "P3"}}, False),
    ("field-regex", {"field": {"name": "status", "regex": "^(open|new)$"}}, {"fields": {"status": "open"}}, True),
    ("field-range", {"field": {"name": "age_days", "min": 2, "max": 5}}, {"fields": {"age_days": 3}}, True),
    ("field-range-miss", {"field": {"name": "age_days", "min": 4}}, {"fields": {"age_days": "3"}}, False),
    ("field-present", {"field": {"name": "owner", "present": False}}, {"fields": {"owner": ""}}, True),
    ("field-list-all", {"field": [{"name": "a", "equals": 1}, {"name": "b", "equals": 2}]},
     {"fields": {"a": 1, "b": 3}}, False),
    ("age-older", {"age": {"older_than": "4h"}}, {"at": "2026-10-15T12:00:00+00:00"}, True),
    ("age-newer", {"age": {"newer_than": "4h"}}, {"at": "2026-10-15T12:00:00+00:00"}, False),
    ("age-undated", {"age": {"older_than": "1m"}}, {"at": ""}, False),
    ("unanswered", {"unanswered": "2h"}, {"handle": "u/300"}, True),
    ("unanswered-too-recent", {"unanswered": "1d"}, {"handle": "u/300"}, False),
    ("unanswered-from-me", {"unanswered": "0m"}, {"handle": "u/100"}, False),
    ("thread-started-by-me", {"thread": {"started_by_me": False}}, {}, True),
    ("reference", {"reference": "incident"}, {"text": "see INC-1234"}, True),
    ("reference-miss", {"reference": "incident"}, {"text": "see ticket"}, False),
    ("all", {"all": [{"source": "chat"}, {"from": {"relation": "chain"}}]}, {"handle": "u/200"}, True),
    ("all-miss", {"all": [{"source": "chat"}, {"from": {"relation": "chain"}}]}, {"handle": "u/300"}, False),
    ("any", {"any": [{"source": "mail"}, {"from": {"relation": "reports"}}]}, {"handle": "u/300"}, True),
    ("not", {"not": {"from": {"is_me": True}}}, {"handle": "u/300"}, True),
    ("implicit-all", {"source": "chat", "from": {"relation": "reports"}}, {"handle": "u/200"}, False),
]


@pytest.mark.parametrize("when,kw,expected", [p[1:] for p in PREDICATES], ids=[p[0] for p in PREDICATES])
def test_predicate(when, kw, expected):
    refs = {"incident": "\\bINC-\\d+\\b"}
    assert fired([rule(when)], item("a", **kw), units=UNITS, references=refs) is expected


def test_thread_predicates_read_the_whole_thread():
    th = "spaces/room/threads/1"
    rows = [item("a", thread=th, handle="u/100", at="2026-10-15T08:00:00+00:00", text="I started this"),
            item("b", thread=th, handle="u/300", at="2026-10-15T09:00:00+00:00", text="a reply"),
            item("c", thread=th, handle="u/100", at="2026-10-15T10:00:00+00:00", text="my answer"),
            item("d", thread="spaces/room/threads/2", handle="u/300", at="2026-10-15T09:00:00+00:00")]
    rules = [rule({"thread": {"started_by_me": True, "i_replied": True, "replies_min": 2}}, rid="mine"),
             rule({"unanswered": "0m"}, rid="waiting"), rule({"thread": {"replies_max": 0}}, rid="lonely")]
    out = {it["id"]: it["signals"]["rules"] for it in run(rules, rows)}
    assert out["b"] == ["mine"]            # answered by the operator later in the thread
    assert out["a"] == ["mine"] and out["c"] == ["mine"]
    assert out["d"] == ["waiting", "lonely"]


EFFECTS = [
    ("importance", {"importance": 30}, lambda s: s["importance"] == 30),
    ("urgency", {"urgency": -15}, lambda s: s["urgency"] == -15),
    ("surface", {"surface": "always"}, lambda s: s["surface"] and s["surface_rules"] == ["r"]),
    ("route", {"route": "backlog"}, lambda s: s["route"] == "backlog"),
    ("area", {"area": "Platform"}, lambda s: s["area"] == "Platform"),
    ("topic", {"topic": "relay"}, lambda s: s["topic"] == "relay"),
    ("sensitivity", {"sensitivity": "customer"}, lambda s: s["sensitivity"] == "customer"),
    ("deadline-field", {"deadline": {"field": "due"}}, lambda s: s["deadline"]["date"] == "2026-10-20"),
    ("deadline-pattern", {"deadline": {"pattern": "by (?P<date>\\w+day)"}},
     lambda s: s["deadline"] == {"raw": "Monday", "date": "2026-10-19", "rule": "r"}),
    ("tags", {"tags": ["a", "b"]}, lambda s: s["tags"] == ["a", "b"]),
    ("dismiss", {"dismiss": "Noise"}, lambda s: s["dismissed"] and s["dismiss"] == [{"rule": "r", "reason": "Noise"}]),
    ("assess", {"assess": "rules_only"}, lambda s: s["assess"] == "rules_only" and s["assess_rules"] == ["r"]),
    ("reason", {"reason": "Because"}, lambda s: s["reasons"] == ["Because"]),
    ("expect_in", {"expect_in": "wiki"}, lambda s: s["expect_in"] == [{"source": "wiki", "reference": None,
                                                                     "rule": "r"}]),
]


@pytest.mark.parametrize("then,check", [e[1:] for e in EFFECTS], ids=[e[0] for e in EFFECTS])
def test_effect(then, check):
    [out] = run([rule({"source": "chat"}, then)], [item("a", text="Ship it by Monday", fields={"due": "2026-10-20"})])
    assert check(out["signals"]), out["signals"]
    entry = out["trace"][-1]
    assert entry["step"] == "rule" and entry["rule"] == "r" and entry["origin"] == "test.yaml:1"


def test_rules_apply_in_file_order_and_all_matching_rules_apply():
    rules = [rule({"source": "chat"}, {"importance": 10, "route": "first", "tags": ["x"]}, "one"),
             rule({"source": "mail"}, {"importance": 99}, "never"),
             rule({"source": "chat"}, {"importance": 5, "route": "second", "tags": ["y", "x"]}, "two")]
    [out] = run(rules, [item("a")])
    sig = out["signals"]
    assert sig["rules"] == ["one", "two"] and sig["importance"] == 15 and sig["tags"] == ["x", "y"]
    assert sig["route"] == "second"  # the later rule's scalar wins and the trace records the override
    assert out["trace"][-1]["overrides"] == {"route": "first"}


def test_surface_always_beats_dismiss_and_the_trace_records_it():
    rules = [rule({"source": "chat"}, {"dismiss": "Automated"}, "bots"),
             rule({"text": "(?i)compliance"}, {"surface": "always"}, "must")]
    keep, drop = run(rules, [item("a", text="Compliance training due"), item("b", text="deploy done")])
    assert not keep["signals"]["dismissed"] and keep["signals"]["surface"]
    over = [e for e in keep["trace"] if e["step"] == "surface_over_dismiss"]
    assert over == [{"stage": "signals", "step": "surface_over_dismiss", "surface_rules": ["must"],
                     "dismissed_by": ["bots"], "reason": "Automated"}]
    assert drop["signals"]["dismissed"]
    assert needs_agent(keep) and not needs_agent(drop)


def test_rules_only_items_need_no_agent():
    [a, b] = run([rule({"field": {"name": "priority", "equals": "P1"}}, {"assess": "rules_only"})],
                 [item("a", fields={"priority": "P1"}), item("b", fields={"priority": "P3"})])
    assert not needs_agent(a) and needs_agent(b)


def test_sender_resolution_and_unknown_is_valid():
    a, b, c, d = run([], [item("a", handle="u/300"), item("b", handle="u/999", **{"from": "Someone"}),
                          item("c", handle="eve@client-co.example"), item("d", handle="u/100")])
    assert a["sender"]["person"] == "tam" and a["sender"]["relation"] == "reports"
    assert "group:core" in a["sender"]["relations"] and "role:report" in a["sender"]["relations"]
    assert b["sender"] == {"handle": "u/999", "display": "Someone", "person": None, "relation": "unknown",
                           "relations": ["unknown"], "is_me": False, "domain": None}
    assert c["sender"]["relation"] == "external" and c["sender"]["domain"] == "client-co.example"
    assert d["sender"]["is_me"] and d["sender"]["relation"] == "me"


def test_plugin_predicate_through_the_registry():
    plugins.register_predicate("long_text", lambda it, args: len(it.get("text") or "") >= args.get("min", 10))
    try:
        rules = [rule({"plugin": {"name": "long_text", "args": {"min": 5}}})]
        assert fired(rules, item("a", text="long enough"))
        assert not fired(rules, item("b", text="tiny"))
    finally:
        plugins.unregister("long_text")


def test_unregistered_or_failing_plugin_is_a_shape_not_a_refusal():
    from docket.shapes.unexpected import collecting

    def boom(it, args):
        raise RuntimeError("bad plugin")
    plugins.register_predicate("boom", boom)
    try:
        with collecting() as col:
            assert not fired([rule({"plugin": "boom"})], item("a"))
            assert not fired([rule({"plugin": "absent_plugin"})], item("a"))
        assert {s.detail for s in col.drain()} == {"plugin predicate boom raised RuntimeError",
                                                   "plugin predicate absent_plugin is not registered"}
    finally:
        plugins.unregister("boom")


def test_entry_point_plugins_are_loaded(monkeypatch):
    class Mod:
        DOCKET_PREDICATES = {"from_entry_point": lambda it, args: True}
        DOCKET_FORMULAS = {"ep_formula": lambda comp, w: 42}

    class EP:
        name = "demo"

        def load(self):
            return Mod

    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: [EP()] if group == plugins.GROUP else [])
    monkeypatch.setattr(plugins, "_LOADED", False)
    try:
        assert plugins.predicate("from_entry_point") is not None
        assert plugins.formula("ep_formula")({}, {}) == 42
    finally:
        plugins.unregister("from_entry_point")
        plugins.unregister("ep_formula")
        monkeypatch.setattr(plugins, "_LOADED", False)


def test_references_extract_named_groups_and_identity():
    refs = {"ticket": {"pattern": "\\b(?P<id>TCK-\\d+)\\b"}, "doc": {"pattern": "https://docs\\.example/\\S+",
                                                                     "fields": ["url"]}}
    [out] = run([], [item("a", identity="TCK-7", text="also TCK-9", url="https://docs.example/p/1")], references=refs)
    assert out["references"] == [{"pattern": "ticket", "value": "TCK-9"}, {"pattern": "ticket", "value": "TCK-7"},
                                 {"pattern": "doc", "value": "https://docs.example/p/1"}]
    assert out["keys"][0] == "ref:ticket:TCK-9"
