"""Strict validation of ``signals`` with file and line (TD §3.4, CFG-02)."""
from __future__ import annotations

from pathlib import Path

import pytest

from docket.config import check
from docket.signals import plugins

BASE = """version: 1
workflows:
  daily: { stages: [collect, assess, merge, decide, close], sources: [chat] }
profile:
  me: { name: Rae Holm, handles: { chat: u/100 } }
  people: [ { id: tam, name: Tam Ober, reports_to: me } ]
  groups: [ { id: core, members: [tam] } ]
  topics: [ { id: relay } ]
sources:
  chat: { kind: files, path: chat.jsonl }
destinations:
  backlog: { adapter: markdown_tasks, path: tasks.md }
"""


def ws(tmp_path: Path, signals: str) -> list[dict]:
    (tmp_path / "docket.yaml").write_text(BASE)
    (tmp_path / "signals.yaml").write_text(signals)
    text = BASE.replace("version: 1\n", "version: 1\nincludes: [signals.yaml]\n")
    (tmp_path / "docket.yaml").write_text(text)
    w, issues, _ = check(tmp_path)
    return [i.as_dict() for i in issues]


def test_a_valid_rule_file(tmp_path):
    assert ws(tmp_path, """signals:
  references: { incident: "\\\\bINC-\\\\d+\\\\b" }
  scoring: { weights: { importance: 0.5, urgency: 0.5 }, floor: 40, caps: { per_source: { default: 5, chat: 3 } } }
  budgets: { daily: 10 }
  rules:
    - id: one
      when: { all: [ { from: { relation: [reports, group:core] } }, { mentions: { topics: [relay] } } ] }
      then: { importance: +10, surface: always, route: backlog, expect_in: { source: chat, reference: incident } }
""") == []


def test_unknown_predicate_names_the_closest_with_file_and_line(tmp_path):
    [e] = ws(tmp_path, """signals:
  rules:
    - id: one
      when:
        all:
          - form: { relation: chain }
      then: { importance: 5 }
""")
    assert e["code"] == "config.unknown_predicate" and e["suggestion"] == "from"
    assert e["file"] == "signals.yaml" and e["line"] == 6 and e["path"] == "signals.rules[0].when.all[0].form"


def test_unknown_effect_and_unknown_key_inside_a_predicate(tmp_path):
    errs = ws(tmp_path, """signals:
  rules:
    - id: one
      when: { from: { relaton: chain } }
      then: { dismis: "noise" }
""")
    by = {e["code"]: e for e in errs}
    assert by["config.unknown_effect"]["suggestion"] == "dismiss" and by["config.unknown_effect"]["line"] == 5
    assert by["config.unknown_key"]["suggestion"] == "relation" and by["config.unknown_key"]["line"] == 4


def test_references_regexes_durations_and_plugins(tmp_path):
    errs = ws(tmp_path, """signals:
  references: { bad: "(unclosed" }
  rules:
    - id: one
      when: { all: [ { from: { relation: chian, person: tom } }, { reference: incdent }, { unanswered: soon },
                     { text: "([" }, { plugin: no_such_plugin }, { source: chatt } ] }
      then: { route: nowhere, expect_in: wiki }
    - id: one
      when: { source: chat }
      then: { tags: [x] }
""")
    got = [(e["code"], e.get("suggestion")) for e in errs]
    assert ("config.bad_reference", "chain") in got and ("config.bad_reference", "tam") in got
    assert ("config.bad_reference", "chat") in got  # unknown source in when, and expect_in: wiki
    assert sum(1 for c, _ in got if c == "config.bad_reference") >= 6
    assert ("config.bad_regex", None) in got and sum(1 for c, _ in got if c == "config.bad_regex") == 2
    assert ("config.bad_type", None) in got  # the duration
    assert any(c == "config.unknown_plugin" for c, _ in got)
    assert ("config.duplicate_id", None) in got


def test_registered_plugin_validates(tmp_path):
    plugins.register_predicate("always_true", lambda it, args: True)
    try:
        assert ws(tmp_path, "signals:\n  rules:\n    - { id: p, when: { plugin: always_true }, then: { tags: [p] } }\n") == []
    finally:
        plugins.unregister("always_true")


@pytest.mark.parametrize("snippet,code", [
    ("  scoring: { weights: { importance: -1 } }\n", "config.bad_type"),
    ("  scoring: { formula: plugin }\n", "config.missing_key"),
    ("  scoring: { floor: 101 }\n", "config.bad_type"),
    ("  rules:\n    - { id: x, when: {}, then: {} }\n", "config.bad_type"),
    ("  rules:\n    - { id: x, when: { source: chat }, then: { deadline: { field: a, pattern: b } } }\n",
     "config.bad_type"),
    ("  budget: { daily: 3 }\n", "config.unknown_key"),
])
def test_semantic_errors(tmp_path, snippet, code):
    assert code in {e["code"] for e in ws(tmp_path, "signals:\n" + snippet)}


def test_shared_destination_with_class_templates_needs_each_assigned_class(tmp_path):
    text = """signals:
  rules:
    - { id: s, when: { source: chat }, then: { sensitivity: personnel } }
"""
    shared = BASE.replace("  backlog: { adapter: markdown_tasks, path: tasks.md }\n",
                          "  backlog: { adapter: markdown_tasks, path: tasks.md, visibility: shared,\n"
                          "             redact: { classes: { customer: Customer matter } } }\n")
    (tmp_path / "signals.yaml").write_text(text)
    (tmp_path / "docket.yaml").write_text(shared.replace("version: 1\n", "version: 1\nincludes: [signals.yaml]\n"))
    _, issues, _ = check(tmp_path)
    [e] = [i.as_dict() for i in issues]
    assert e["code"] == "config.missing_key" and "'personnel'" in e["message"] and e["file"] == "docket.yaml"
