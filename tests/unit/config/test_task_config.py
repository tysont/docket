"""The ``tasks`` configuration section (TD §9.1): budgets, vocabulary and workspace kinds."""
from __future__ import annotations

import textwrap

import pytest

from docket.config import check

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
graphs:
  daily:
    read-notes: { read: notes }
    assess: { judge: assess, from: [read-notes] }
    merge: { merge: [assess] }
profile:
  me: { name: Pat Example }
sources:
  notes: { kind: files, path: data/notes.jsonl }
"""

KIND = """\
tasks:
  kinds:
    mood:
      instructions: "Judge the mood of each note sent to {me.name}."
      fields:
        mood: { type: enum, description: The mood., required: true, values: { good: Fine., bad: Not fine. }, safe: good }
"""


def issues(tmp_path, text):
    (tmp_path / "docket.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    ws, found, _ = check(tmp_path)
    return ws, [(i.code, i.path, i.line) for i in found]


MOOD = "    mood: { judge: mood, from: [read-notes] }\n    merge: { merge: [assess, mood] }\n"


def with_mood(base):
    return base.replace("    merge: { merge: [assess] }\n", MOOD)


def test_a_workspace_kind_validates_and_defaults_apply(tmp_path):
    ws, found = issues(tmp_path, with_mood(BASE) + KIND)
    assert found == [] and ws is not None
    t = ws.config.tasks
    assert t.concurrency == 6 and t.assess.max_items == 10 and t.kinds["mood"].family == "item"
    assert ws.config.graphs["daily"]["mood"].judge == "mood"


def test_a_review_family_kind_is_accepted(tmp_path):
    ws, found = issues(tmp_path, BASE + KIND.replace("    mood:\n", "    mood:\n      family: review\n"))
    assert found == [] and ws.config.tasks.kinds["mood"].family == "review"


@pytest.mark.parametrize("bad,code,path", [
    ("tasks:\n  concurency: 3\n", "config.unknown_key", "tasks.concurency"),
    ("tasks:\n  concurrency: 0\n", "config.bad_type", "tasks.concurrency"),
    (KIND.replace("type: enum", "type: float"), "config.bad_type", "tasks.kinds.mood.fields.mood.type"),
    (KIND.replace(", values: { good: Fine., bad: Not fine. }, safe: good", ""), "config.bad_type",
     "tasks.kinds.mood.fields.mood"),
    (KIND.replace("safe: good", "safe: meh"), "config.bad_type", "tasks.kinds.mood.fields.mood"),
    (KIND.replace("    mood:\n", "    mood:\n      family: rewiew\n"), "config.bad_type", "tasks.kinds.mood.family"),
    (KIND.replace("{me.name}", "{me.nam}"), "config.bad_template", "tasks.kinds.mood.instructions"),
    (KIND.replace("    mood:\n      instructions", "    assess:\n      instructions"), "config.bad_type", "tasks.kinds"),
    (KIND.replace("        mood: {", "        source: {"), "config.bad_type", "tasks.kinds.mood"),
])
def test_task_config_errors_name_the_path(tmp_path, bad, code, path):
    _, found = issues(tmp_path, BASE + bad)
    assert (code, path) in [(c, p) for c, p, _ in found], found
    assert all(line for c, p, line in found if p == path)


def test_a_judge_node_must_name_a_task_kind(tmp_path):
    _, found = issues(tmp_path, with_mood(BASE).replace("judge: mood", "judge: mod") + KIND)
    code, path = "config.bad_reference", "graphs.daily.mood.judge"
    assert (code, path) in [(c, p) for c, p, _ in found], found
    assert all(line for c, p, line in found if p == path)
