"""The agenda audit's settings (TD §5.1, §9.7, §10.7, §12.8): a judge: audit node says which
forums it audits, an ask node asks about its proposals, and a write: audit node writes the
answers. The example graph of the design validates as written. All names are invented."""
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
    gather-meetings: { read: meetings }
    decide: { ask: default, from: [gather-meetings] }
    plan: { plan: [decide] }
    approve-writes: { approve: plan }
    apply-writes: { apply: approve-writes }
    audit-agendas: { judge: audit, from: [gather-meetings], after: [apply-writes], audit: { after_meeting: true, before_meeting: 2d } }
    review-agendas: { ask: default, questions: { kinds: [audit], audit: { from: audit-agendas, to: agendas } } }
    update-agendas: { write: audit, to: agendas, from: [review-agendas] }
sources:
  meetings: { kind: files, path: meetings.jsonl }
profile:
  timezone: UTC
  me: { name: Workspace User }
  people:
    - { id: lin, name: Lin Doe, about: "Runs the storage team." }
  forums:
    - { id: lin-weekly, name: Lin weekly, person: lin, agenda: { destination: agendas, path: Syncs/Lin.md } }
destinations:
  backlog: { adapter: markdown_tasks, path: tasks.md }
  agendas: { adapter: markdown_agenda, path: Syncs }
tasks:
  audit: { agendas: agendas, max_add: 2, max_ledger: 10, guidance: "Prefer decisions over status." }
"""


def issues_of(tmp_path: Path, text: str):
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "docket.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    _, issues, _ = check(root)
    return issues


def messages(tmp_path, text):
    return [i.message for i in issues_of(tmp_path, text)]


def test_the_example_graph_validates_and_the_ask_node_waits_for_the_audit(tmp_path):
    assert issues_of(tmp_path, BASE) == []
    node = Node.model_validate({"ask": "default", "questions": {"kinds": ["audit"],
                                                                "audit": {"from": "audit-agendas", "to": "agendas"}}})
    assert node.needs == ["audit-agendas"] and node.question_sources == ["audit-agendas"]
    judge = Node.model_validate({"judge": "audit", "audit": {"before_meeting": "2d", "forums": ["lin-weekly"]}})
    assert judge.audit.after_meeting is True and judge.audit.before_meeting == "2d"


def test_the_audit_kind_needs_its_settings_and_the_settings_need_the_kind(tmp_path):
    msgs = messages(tmp_path, BASE.replace(", audit: { from: audit-agendas, to: agendas }", ""))
    assert any("the audit kind needs questions.audit" in m for m in msgs), msgs
    msgs = messages(tmp_path, BASE.replace("kinds: [audit]", "kinds: [finding]"))
    assert any("questions.audit is read only when kinds lists audit" in m for m in msgs), msgs


def test_audit_settings_belong_to_a_judge_audit_node_and_its_forums_must_exist(tmp_path):
    msgs = messages(tmp_path, BASE.replace("decide: { ask: default, from: [gather-meetings] }",
                                           "decide: { ask: default, from: [gather-meetings], audit: {} }"))
    assert any("only a judge: audit node takes it" in m for m in msgs), msgs
    issues = issues_of(tmp_path, BASE.replace("before_meeting: 2d }", "before_meeting: 2d, forums: [lin-weekley] }"))
    (ref,) = [i for i in issues if i.code == "config.bad_reference"]
    assert ref.path == "graphs.agendas.audit-agendas.audit.forums[0]" and ref.suggestion == "lin-weekly"
    msgs = messages(tmp_path, BASE.replace("before_meeting: 2d", "before_meeting: soon"))
    assert any("before_meeting" in m for m in msgs), msgs


def test_the_ask_node_reads_a_judge_audit_node_and_the_write_reads_an_audit_ask(tmp_path):
    msgs = messages(tmp_path, BASE.replace("audit: { from: audit-agendas, to: agendas }",
                                           "audit: { from: gather-meetings, to: agendas }"))
    assert any("questions.audit.from names a judge: audit node" in m for m in msgs), msgs
    msgs = messages(tmp_path, BASE.replace("update-agendas: { write: audit, to: agendas, from: [review-agendas] }",
                                           "update-agendas: { write: audit, to: agendas, from: [decide] }"))
    assert any("works on the answers of an ask node that lists the audit kind" in m for m in msgs), msgs


@pytest.mark.parametrize("change,path", [
    (("audit: { from: audit-agendas, to: agendas }", "audit: { from: audit-agendas, to: backlog }"),
     "graphs.agendas.review-agendas.questions.audit.to"),
    (("update-agendas: { write: audit, to: agendas", "update-agendas: { write: audit, to: backlog"),
     "graphs.agendas.update-agendas.to"),
    (("audit: { agendas: agendas,", "audit: { agendas: backlog,"), "tasks.audit.agendas"),
])
def test_the_audit_reads_and_writes_an_agenda(tmp_path, change, path):
    issues = issues_of(tmp_path, BASE.replace(*change))
    caps = [i for i in issues if i.code == "config.capability_missing"]
    assert [i.path for i in caps] == [path], [(i.code, i.path, i.message) for i in issues]
    assert "markdown_agenda" in caps[0].message


def test_a_judge_audit_node_needs_the_agendas_it_reads(tmp_path):
    issues = issues_of(tmp_path, BASE.replace("  audit: { agendas: agendas, max_add: 2,", "  audit: { max_add: 2,"))
    (cap,) = [i for i in issues if i.code == "config.capability_missing"]
    assert "tasks.audit.agendas names no destination" in cap.message or "tasks.audit.agendas" in cap.message


def test_roll_settings_stay_on_a_roll(tmp_path):
    msgs = messages(tmp_path, BASE.replace("write: audit, to: agendas,", "write: audit, to: agendas, trailer: x,"))
    assert any("trailer" in m for m in msgs), msgs


def test_the_audit_labels_and_task_settings_are_checked(tmp_path):
    text = BASE + "decisions:\n  audit:\n    labels: { accept: \"Update {forum.name}\", skip: \"Leave it\" }\n"
    assert issues_of(tmp_path, text) == []
    msgs = messages(tmp_path, text.replace("skip: \"Leave", "skipped: \"Leave"))
    assert any("unknown audit option(s) ['skipped']" in m for m in msgs), msgs
    msgs = messages(tmp_path, BASE.replace("max_add: 2", "max_add: 11"))
    assert any("max_add" in m for m in msgs), msgs
