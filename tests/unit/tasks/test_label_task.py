"""The label task: the words that stand for a task where other people can read them.

Whether a title is safe to show is a judgment, not a pattern: "Review the storage Q3 plan"
is fine and "Conduct a performance review for a named person" is not. So the words are
written by a judged task with the workspace's own guidance and worked examples, once per
task line, and reused until the line changes."""
from __future__ import annotations

import sqlite3

import pytest

from docket.config.digest import digest
from docket.config.task_models import TasksConfig
from docket.plan import labels
from docket.store.db import open_db
from docket.tasks import submit
from docket.tasks.kinds import registry
from tests.task_helpers import packs_for, persona_config, raw_from_lines

pytestmark = pytest.mark.smoke
RERUN = ["docket", "task", "submit", "t_x", "--file", "r.json", "--json"]
CFG = {"label": {"guidance": "Say the kind of work. Never name a person.",
                 "examples": [{"shows": "A task to review a product plan.", "row": {"label": "Review a product plan"},
                               "why": "The plan is not confidential."},
                              {"shows": "A task about one person's performance.", "row": {"label": "Private work"}}]}}


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_the_kind_asks_for_one_short_label_and_carries_the_workspaces_examples():
    kind = registry(TasksConfig.model_validate(CFG))["label"]
    assert kind.family == "label" and kind.core
    spec = {f.name: f for f in kind.fields}["label"]
    assert spec.required and spec.max_length == 60
    assert "Never name a person" in (kind.guidance or "")
    assert ["Review a product plan" in kind.examples[0][1], "Private work" in kind.examples[1][1]] == [True, True]
    assert "other people can read" in kind.instructions


def test_a_pack_shows_the_task_line_and_a_label_is_accepted_or_refused():
    kind = registry(TasksConfig.model_validate(CFG))["label"]
    it = labels.candidate("t_1", "Review the Kestrel plan. Morgan Ito wrote it", "backlog", area="Platform")
    pack = packs_for(kind, [it], persona_config("reference-like"))[0]
    view = pack["items"][0]
    assert view["id"] == "t_1" and view["lines"][0]["text"] == "Review the Kestrel plan. Morgan Ito wrote it"
    assert view["fields"] == {"area": "Platform"}
    ok = submit.validate(kind, pack, {"items": [{"id": "t_1", "label": "Review a product plan"}]},
                         raw_text=raw_from_lines(pack), rerun=RERUN)
    assert ok.body["items"][0]["label"] == "Review a product plan"
    opened = submit.validate(kind, pack, {"items": [{"id": "t_1", "label": "Review a product plan", "open": True}]},
                             raw_text=raw_from_lines(pack), rerun=RERUN)
    assert opened.body["items"][0]["open"] is True and not ok.body["items"][0].get("open")   # left out, it is false
    from docket.cli.errors import DocketError
    with pytest.raises(DocketError):
        submit.validate(kind, pack, {"items": [{"id": "t_1"}]}, raw_text=raw_from_lines(pack), rerun=RERUN)
    with pytest.raises(DocketError):
        submit.validate(kind, pack, {"items": [{"id": "t_1", "label": "x" * 61}]}, raw_text=raw_from_lines(pack), rerun=RERUN)


def test_a_label_is_written_once_per_line_and_again_when_the_line_changes(db):
    tasks = [("t_1", "Review the plan"), ("t_2", "Reply on the thread")]
    assert [c["id"] for c in labels.pending(db, tasks, "backlog")] == ["t_1", "t_2"]
    with db:
        labels.remember(db, {"t_1": "Review a plan"}, dict(tasks), run_id="r1")
    assert labels.known(db, dict(tasks)) == {"t_1": "Review a plan"}
    assert [c["id"] for c in labels.pending(db, tasks, "backlog")] == ["t_2"]
    changed = [("t_1", "Review the plan and the budget"), ("t_2", "Reply on the thread")]
    assert labels.known(db, dict(changed)) == {}                              # the old words may no longer fit
    assert [c["id"] for c in labels.pending(db, changed, "backlog")] == ["t_1", "t_2"]
    assert digest("Review the plan") != digest("Review the plan and the budget")
