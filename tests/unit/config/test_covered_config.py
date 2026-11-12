"""The ``covered`` question kind's settings (TD §10.4, §12.8): an ask node names the read node of
the meetings and the agenda destination, and a workspace either assumes a meeting with no
record covered its agenda (``write: roll`` with ``no_record``) or asks, never both."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from docket.config import check
from docket.config.models import Node

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
graphs:
  agendas:
    read-meetings: { read: meetings }
    decide: { ask: default, questions: { kinds: [covered, finding], covered: { meetings: read-meetings, to: agendas, suffix: "no record" } } }
    plan: { plan: [decide] }
    approve: { approve: plan }
    apply: { apply: approve }
sources:
  meetings: { kind: files, path: meetings.jsonl }
profile:
  timezone: UTC
  me:
    name: Workspace User
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
  agendas:
    adapter: markdown_agenda
    path: Syncs
"""


def ws_with(tmp_path: Path, text: str) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "docket.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def issues_of(tmp_path, text):
    _, issues, _ = check(ws_with(tmp_path, text))
    return issues


def test_the_covered_settings_validate_and_the_ask_node_waits_for_the_meetings(tmp_path):
    assert issues_of(tmp_path, BASE) == []
    node = Node.model_validate({"ask": "default", "questions": {
        "kinds": ["covered"], "covered": {"meetings": "read-meetings", "to": "agendas"}}})
    assert node.needs == ["read-meetings"] and node.inputs == []
    assert node.questions.covered.suffix == "no record of the meeting"


def test_the_covered_kind_needs_its_settings_and_the_settings_need_the_kind(tmp_path):
    msgs = [i.message for i in issues_of(tmp_path, BASE.replace(
        ", covered: { meetings: read-meetings, to: agendas, suffix: \"no record\" }", ""))]
    assert any("the covered kind needs questions.covered" in m for m in msgs), msgs
    msgs = [i.message for i in issues_of(tmp_path, BASE.replace("kinds: [covered, finding]", "kinds: [finding]"))]
    assert any("questions.covered is read only when kinds lists covered" in m for m in msgs), msgs


def test_an_unknown_question_kind_is_refused(tmp_path):
    msgs = [i.message for i in issues_of(tmp_path, BASE.replace("kinds: [covered, finding]",
                                                                "kinds: [covered, findings]"))]
    assert any("unknown question kind 'findings'" in m for m in msgs), msgs


@pytest.mark.parametrize("meetings,message", [
    ("read-notes", "node decide names 'read-notes', which is not a node of the graph"),
    ("notes", "node decide (ask) reads the meetings of notes (run); questions.covered.meetings names a read node"),
])
def test_the_meetings_node_must_be_a_read_node_of_the_graph(tmp_path, meetings, message):
    text = BASE.replace("    plan: { plan: [decide] }\n", "    notes: { run: [\"true\"] }\n    plan: { plan: [decide] }\n")
    issues = issues_of(tmp_path, text.replace("meetings: read-meetings", f"meetings: {meetings}"))
    bad = [i for i in issues if i.code == "config.bad_graph"]
    assert any(message in i.message for i in bad), [i.message for i in issues]
    assert all(i.path == "graphs.agendas.decide" for i in bad if message in i.message)


def test_the_destination_must_exist_and_be_an_agenda(tmp_path):
    issues = issues_of(tmp_path, BASE.replace("to: agendas, suffix", "to: agenda, suffix"))
    (ref,) = [i for i in issues if i.code == "config.bad_reference"]
    assert ref.path == "graphs.agendas.decide.questions.covered.to" and ref.suggestion == "agendas"
    issues = issues_of(tmp_path, BASE.replace("to: agendas, suffix", "to: backlog, suffix"))
    (cap,) = [i for i in issues if i.code == "config.capability_missing"]
    assert cap.path == "graphs.agendas.decide.questions.covered.to"
    assert "backlog is a markdown_tasks destination; covered questions roll an agenda (markdown_agenda)" in cap.message


def test_assuming_and_asking_about_the_same_meetings_is_refused(tmp_path):
    roll = "    roll: { write: roll, from: [read-meetings], to: agendas, no_record: \"assumed covered\" }\n"
    text = BASE.replace("    plan: { plan: [decide] }\n", roll + "    plan: { plan: [decide] }\n")
    issues = issues_of(tmp_path, text)
    (bad,) = [i for i in issues if i.code == "config.conflicting_settings"]
    assert bad.path == "graphs.agendas.roll.no_record" and bad.file == "docket.yaml"
    assert "node roll assumes a meeting with no record covered its agenda (no_record), and node decide asks " \
           "about the same meetings (covered)" in bad.message
    # a roll that leaves meetings with no record alone sits beside the question
    assert issues_of(tmp_path, text.replace(", no_record: \"assumed covered\"", "")) == []
    # so does one that reads other meetings
    other = text.replace("from: [read-meetings], to: agendas, no_record",
                         "from: [read-calls], to: agendas, no_record").replace(
        "    read-meetings: { read: meetings }\n",
        "    read-meetings: { read: meetings }\n    read-calls: { read: calls }\n").replace(
        "  meetings: { kind: files, path: meetings.jsonl }\n",
        "  meetings: { kind: files, path: meetings.jsonl }\n  calls: { kind: files, path: calls.jsonl }\n")
    assert issues_of(tmp_path, other) == []


def test_the_order_of_kinds_and_the_labels_of_covered_are_configurable(tmp_path):
    text = BASE + textwrap.dedent("""\
        decisions:
          order: [covered, reconcile, finding, verify]
          covered:
            labels: { covered: "Covered on {meeting.date}", keep: "Keep it for {forum.name}" }
          templates:
            covered_prompt: "{forum.name} {meeting.date}: {entry.text}"
        """)
    assert issues_of(tmp_path, text) == []
    issues = issues_of(tmp_path, text.replace("keep: \"Keep", "kept: \"Keep"))
    assert any("unknown covered option(s) ['kept']" in i.message for i in issues), [i.message for i in issues]
    issues = issues_of(tmp_path, text.replace("{entry.text}", "{entry.title}"))
    assert [i.code for i in issues] == ["config.bad_template"]


@pytest.mark.parametrize("setting,message", [
    ("overflow: keep", "questions.overflow: Input should be 'list' or 'drop'"),
    ("budget: -1", "questions.budget: Input should be greater than or equal to 0"),
    ("batch_budget: -2", "questions.batch_budget: Input should be greater than or equal to 0"),
])
def test_overflow_and_the_budgets_are_validated_at_the_node(tmp_path, setting, message):
    issues = issues_of(tmp_path, BASE.replace("kinds: [covered, finding]", f"kinds: [covered, finding], {setting}"))
    assert any(i.code == "config.bad_type" and message in i.message for i in issues), issues
    ok = BASE.replace("kinds: [covered, finding]", "kinds: [covered, finding], budget: null, batch_budget: null, "
                                                   "overflow: drop")
    assert issues_of(tmp_path / "ok", ok) == []
