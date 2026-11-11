"""Unit rules can use the profile's vocabulary (TD §6.9). A unit whose name mentions an
owned topic or a known entity is classified by what the profile says, so adding a topic
or an entity to the profile classifies its units with no new rule."""
from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from docket.config import check
from docket.sources.base import Unit
from docket.sources.units import classify, unit_rules

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
graphs:
  daily: { touch: { run: ["true"] } }
profile:
  timezone: UTC
  me: { name: Dana Reyes }
  topics:
    - { id: ingest, aliases: [Ingest Service, IS], owned: true }
    - { id: billing, aliases: [Billing], owned: false }
  entities:
    - { id: acme, aliases: [Acme Corp], strategic: true, protected: true }
    - { id: globex, aliases: [Globex] }
signals:
  units:
    rules:
"""

RULES = """\
      - { id: owned, match: { mentions: { topics: owned } }, set: { class: service, tier: 2 } }
      - { id: key-account, match: { mentions: { entities: strategic } }, set: { class: customer, tier: 2 } }
      - { id: account, match: { mentions: { entities: any } }, set: { class: customer, tier: 3 } }
      - { id: billing, match: { mentions: { topics: [billing] } }, set: { class: adjacent, tier: 3 } }
      - { id: both, match: { name_regex: "Escalation", mentions: { entities: any } }, set: { kind: escalation } }
"""


def load(tmp_path, rules=RULES):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docket.yaml").write_text(BASE + rules, encoding="utf-8")
    ws, issues, _ = check(root)
    return ws, issues


def cls(ws, name):
    got, used = classify(unit_rules(ws), "chat", Unit("u1", name), ws)
    return got.get("class"), got.get("tier"), used


def test_a_unit_named_after_an_owned_topic_or_an_entity_is_classified_from_the_profile(tmp_path):
    ws, issues = load(tmp_path)
    assert issues == []
    assert cls(ws, "Ingest Service (Team Only)") == ("service", 2, ["owned"])
    assert cls(ws, "IS oncall") == ("service", 2, ["owned"])            # a short all-caps alias, case-sensitively
    assert cls(ws, "This is fine") == (None, None, [])                    # and not inside ordinary words
    assert cls(ws, "Billing questions") == ("adjacent", 3, ["billing"])   # not owned: only the rule naming it
    assert cls(ws, "Acme Corp account team") == ("customer", 2, ["key-account"])
    assert cls(ws, "Globex onboarding") == ("customer", 3, ["account"])
    assert cls(ws, "Lunch") == (None, None, [])


def test_every_condition_of_a_rule_must_hold(tmp_path):
    ws, _ = load(tmp_path)
    got, used = classify(unit_rules(ws), "chat", Unit("u1", "Globex Escalation"), ws)
    assert got == {"class": "customer", "tier": 3, "kind": "escalation"} and used == ["account", "both"]
    got, _ = classify(unit_rules(ws), "chat", Unit("u1", "Escalation process"), ws)
    assert "kind" not in got


def test_rules_without_vocabulary_need_no_profile(tmp_path):
    ws, _ = load(tmp_path, '      - { id: war, match: { name_regex: "war room" }, set: { class: incident } }\n')
    assert classify(unit_rules(ws), "chat", Unit("u1", "ingest war room"))[0] == {"class": "incident"}


@pytest.mark.parametrize("rule,path", [
    ('match: { mentions: { topics: [nope] } }', "signals.units.rules[0].match.mentions.topics"),
    ('match: { mentions: { entities: [nope] } }', "signals.units.rules[0].match.mentions.entities"),
    ('match: { mentions: { topics: strategic } }', "signals.units.rules[0].match.mentions.topics"),
])
def test_a_vocabulary_reference_must_name_something_the_profile_has(tmp_path, rule, path):
    _, issues = load(tmp_path, f"      - {{ id: bad, {rule}, set: {{ class: x }} }}\n")
    assert [i.path for i in issues] == [path] and issues[0].code in ("config.bad_reference", "config.bad_type")
