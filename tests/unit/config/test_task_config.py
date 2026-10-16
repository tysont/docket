"""The ``tasks`` configuration section (TD §9.1): budgets, vocabulary and workspace kinds."""
from __future__ import annotations

import textwrap

import pytest

from docket.config import check

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
workflows:
  daily:
    stages: [collect, assess, merge, decide, close]
profile:
  me: { name: Pat Example }
sources:
  notes: { kind: files, path: data/notes.jsonl }
"""

KIND = """\
tasks:
  kinds:
    mood:
      select: { sources: [notes] }
      instructions: "Judge the mood of each note sent to {me.name}."
      fields:
        mood: { type: enum, description: The mood., required: true, values: { good: Fine., bad: Not fine. }, safe: good }
"""


def issues(tmp_path, text):
    (tmp_path / "docket.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    ws, found, _ = check(tmp_path)
    return ws, [(i.code, i.path, i.line) for i in found]


def test_a_workspace_kind_validates_and_defaults_apply(tmp_path):
    ws, found = issues(tmp_path, BASE + KIND)
    assert found == [] and ws is not None
    t = ws.config.tasks
    assert t.concurrency == 6 and t.assess.max_items == 10 and t.kinds["mood"].stage == "assess"


@pytest.mark.parametrize("bad,code,path", [
    ("tasks:\n  concurency: 3\n", "config.unknown_key", "tasks.concurency"),
    ("tasks:\n  concurrency: 0\n", "config.bad_type", "tasks.concurrency"),
    (KIND.replace("type: enum", "type: float"), "config.bad_type", "tasks.kinds.mood.fields.mood.type"),
    (KIND.replace(", values: { good: Fine., bad: Not fine. }, safe: good", ""), "config.bad_type",
     "tasks.kinds.mood.fields.mood"),
    (KIND.replace("safe: good", "safe: meh"), "config.bad_type", "tasks.kinds.mood.fields.mood"),
    (KIND.replace("sources: [notes]", "sources: [nots]"), "config.bad_reference", "tasks.kinds.mood.select.sources[0]"),
    (KIND.replace("{me.name}", "{me.nam}"), "config.bad_template", "tasks.kinds.mood.instructions"),
    (KIND.replace("    mood:\n      select", "    assess:\n      select"), "config.bad_type", "tasks.kinds"),
    (KIND.replace("        mood: {", "        source: {"), "config.bad_type", "tasks.kinds.mood"),
])
def test_task_config_errors_name_the_path(tmp_path, bad, code, path):
    _, found = issues(tmp_path, BASE + bad)
    assert (code, path) in [(c, p) for c, p, _ in found], found
    assert all(line for c, p, line in found if p == path)
