"""Adopting task lines that carry no key (TD §11.5, §13.8). A task list is edited by hand and
by other tools, which add open lines in its grammar without the ledger key. A destination
with ``adopt`` gives such a line its key: the same words, links and fields, plus the key
and any required field that is missing and has a default. The item's topic is the one
``docket item add`` derives from the same words and first link."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from docket.config import load_workspace
from docket.destinations import build, env_for
from docket.items import adopt, add, ledger
from docket.store.db import open_db
from docket.text.grammar import Grammar

pytestmark = pytest.mark.smoke

TODAY, WORKDAYS = "2026-10-15", {0, 1, 2, 3, 4}        # a Thursday
T1 = "2026-10-15T08:00:00+00:00"

CONFIG = """\
version: 1
profile:
  timezone: UTC
  me: { name: Sam Doe, handles: { mail: sam@example.org } }
  workdays: [mon, tue, wed, thu, fri]
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
    section: "## Inbox"
    sections: { inbox: "## Inbox", priority: "## Priority" }
    adopt: true
    quality:
      imperative: { deny: [fyi], default_verb: "Follow up on" }
      require: [link, due, est, area, key]
      optional: [due]
      fields: { key: id }
      defaults: { est: 30m, area: Home }
"""
LINKED = "- [ ] Watch the storage talk [video](https://video.example.org/v/1) [due:: 2026-10-20] [est:: 45m]"
TASKS = f"""# Tasks

## Inbox
{LINKED}
- [ ] fyi call the dentist
- [x] Renew the passport
- [ ] Review the runbook [id:: iseed0000]

## Priority
- [ ] Book the venue [area:: Events]

## Someday
- [ ] Learn the cello
"""


def workspace(tmp_path: Path, config: str = CONFIG, tasks: str = TASKS):
    (tmp_path / "docket.yaml").write_text(config, encoding="utf-8")
    (tmp_path / "tasks.md").write_text(tasks, encoding="utf-8")
    return load_workspace(tmp_path)


def scan(ws, lookup=None):
    dest = build("backlog", ws.config.destinations["backlog"], env_for(ws))
    try:
        return adopt.scan(dest, dest.snapshot(), today=TODAY, workdays=WORKDAYS, lookup=lookup)
    finally:
        dest.close()


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_the_words_of_a_line_leave_out_its_links_and_fields():
    g = Grammar()
    t = g.parse(LINKED)
    assert t.words == "Watch the storage talk" and t.links[0] == "https://video.example.org/v/1"
    t = g.parse("- [ ] Read https://docs.example.org/a and [the notes](https://notes.example.org/n) [est:: 1h]")
    assert t.words == "Read and" and t.links[0] == "https://notes.example.org/n"
    assert g.parse("- [ ] Call [[Pat Doe|Pat]] about [[Budget]]").words == "Call Pat about Budget"


def test_an_open_line_without_a_key_gets_the_key_item_add_would_give_it(tmp_path):
    ws = workspace(tmp_path)
    found = scan(ws)
    assert [a.old_line for a in found] == [LINKED, "- [ ] fyi call the dentist", "- [ ] Book the venue [area:: Events]"]
    a, b, c = found
    key = add.key_of("Watch the storage talk", "https://video.example.org/v/1")
    assert (a.key, a.topic) == (key, add.topic_of("Watch the storage talk", "https://video.example.org/v/1"))
    # the words, links and fields as they were; the missing required fields with a default, in
    # the order quality.require gives them; the optional due stays absent where it is absent
    assert a.line == LINKED + f" [area:: Home] [id:: {key}]"
    assert (a.title, a.url, a.due, a.est, a.area) == ("Watch the storage talk", "https://video.example.org/v/1",
                                                      "2026-10-20", 45, "Home")
    assert (a.destination, a.path, a.anchor, a.was) == ("backlog", "tasks.md", "## Inbox", None)
    # no imperative rewrite and no refusal for a missing link: the line is the operator's own
    assert b.line == f"- [ ] fyi call the dentist [est:: 30m] [area:: Home] [id:: {add.key_of('fyi call the dentist', None)}]"
    assert (b.url, b.due, b.est) == (None, None, 30)
    assert c.anchor == "## Priority" and c.line == f"- [ ] Book the venue [area:: Events] [est:: 30m] [id:: {c.key}]"
    assert c.area == "Events"


def test_a_section_the_destination_does_not_own_and_a_closed_line_are_left_alone(tmp_path):
    ws = workspace(tmp_path)
    lines = [a.old_line for a in scan(ws)]
    assert not any("cello" in ln or "passport" in ln or "runbook" in ln for ln in lines)


def test_without_sections_the_destination_owns_its_one_section(tmp_path):
    one = CONFIG.replace('    sections: { inbox: "## Inbox", priority: "## Priority" }\n', "")
    ws = workspace(tmp_path, one)
    assert [a.anchor for a in scan(ws)] == ["## Inbox", "## Inbox"]


def test_the_field_names_and_duration_style_are_the_destinations(tmp_path):
    named = CONFIG.replace("fields: { key: id }", "fields: { key: ref, est: size }\n      duration: minutes")
    ws = workspace(tmp_path, named, "## Inbox\n- [ ] Plan the offsite [:: 2026-10-30]\n")
    [a] = scan(ws)
    assert a.line == f"- [ ] Plan the offsite [:: 2026-10-30] [size:: 30m] [area:: Home] [ref:: {a.key}]"
    assert a.est == 30


def test_a_line_that_carries_its_key_is_not_adopted_again(tmp_path):
    ws = workspace(tmp_path)
    text = TASKS
    for a in scan(ws):
        text = text.replace(a.old_line + "\n", a.line + "\n")
    (tmp_path / "tasks.md").write_text(text, encoding="utf-8")
    assert scan(ws) == []


def test_the_ledger_row_says_where_it_stands(tmp_path):
    ws = workspace(tmp_path)
    a = scan(ws, lookup=lambda key: {"state": "done"} if key.startswith("i") else None)[0]
    assert a.was == "done"
    assert "reopen" in adopt.describe(a.op())


def test_an_applied_adoption_enters_the_item_in_backlog(db, tmp_path):
    ws = workspace(tmp_path)
    a = scan(ws)[0]
    op = a.op()
    assert (op["verb"], op["old_line"], op["line"], op["item_id"]) == ("replace", LINKED, a.line, a.key)
    moves = adopt.enter(db, [op], {op["op_id"]: {"outcome": "applied"}}, run_id="r1", at=T1)
    assert [(m["key"], m["state"]) for m in moves] == [(a.key, "backlog")]
    row = ledger.get(db, a.key)
    assert (row["state"], row["topic"], row["title"], row["url"], row["source"]) == \
        ("backlog", a.topic, "Watch the storage talk", "https://video.example.org/v/1", "hand")
    assert (row["due"], row["est"], row["area"]) == ("2026-10-20", 45, "Home")
    [p] = row["placements"]
    assert (p["destination"], p["adapter"], p["path"], p["anchor"], p["line"], p["op_id"], p["run"]) == \
        ("backlog", "markdown_tasks", "tasks.md", "## Inbox", a.line, op["op_id"], "r1")
    assert row["history"] == [{"run": "r1", "at": T1, "decision": "adopted", "state": "backlog",
                               "note": "adopted from backlog"}]


def test_an_adoption_that_was_not_applied_enters_nothing(db, tmp_path):
    ws = workspace(tmp_path)
    op = scan(ws)[0].op()
    assert adopt.enter(db, [op], {op["op_id"]: {"outcome": "refused"}}, run_id="r1", at=T1) == []
    assert adopt.enter(db, [op], {}, run_id="r1", at=T1) == []
    assert ledger.rows(db) == []


def test_an_open_row_with_the_topic_gets_the_placement_and_keeps_its_history(db, tmp_path):
    ws = workspace(tmp_path)
    a = scan(ws)[0]
    ledger.put(db, ledger.added(db, {"key": a.key, "topic": a.topic, "source": "hand", "title": a.title,
                                     "url": a.url, "due": None, "est": None, "area": None,
                                     "placement": {"destination": "backlog", "adapter": "markdown_tasks",
                                                   "line": "an older line"}},
                                run_id="add-2026-10-01", at="2026-10-01T08:00:00+00:00", note=None))
    adopt.enter(db, [a.op()], {a.op()["op_id"]: {"outcome": "applied"}}, run_id="r1", at=T1)
    row = ledger.get(db, a.key)
    assert row["state"] == "backlog" and row["first_run"] == "add-2026-10-01"
    assert [p["line"] for p in row["placements"]] == ["an older line", a.line]
    assert [h["decision"] for h in row["history"]] == ["added", "adopted"]


@pytest.mark.parametrize("closed", ["done", "ignored"])
def test_a_done_or_ignored_row_is_reopened_because_an_open_line_was_found(db, tmp_path, closed):
    """The line is open in the operator's own list: leaving the ledger saying done would lose
    it, and refusing would leave it untracked. So the row goes back to backlog and says why."""
    ws = workspace(tmp_path)
    a = scan(ws)[0]
    ledger.put(db, ledger.added(db, {"key": a.key, "topic": a.topic, "source": "hand", "title": a.title,
                                     "url": a.url, "due": None, "est": None, "area": None, "placement": None},
                                run_id="add-2026-10-01", at="2026-10-01T08:00:00+00:00", note=None))
    ledger.edit(db, a.key, {"state": closed}, note=None, at="2026-10-02T08:00:00+00:00")
    a = scan(ws, lookup=lambda key: ledger.get(db, key))[0]
    assert a.was == closed
    moves = adopt.enter(db, [a.op()], {a.op()["op_id"]: {"outcome": "applied"}}, run_id="r1", at=T1)
    assert moves == [{"key": a.key, "title": a.title, "was": closed, "state": "backlog",
                      "note": f"an open line was found in backlog; reopened from {closed}"}]
    row = ledger.get(db, a.key)
    assert row["state"] == "backlog" and row["history"][-1]["note"].startswith("an open line was found")


def issues_of(tmp_path: Path, config: str) -> list:
    from docket.config import check
    (tmp_path / "docket.yaml").write_text(config, encoding="utf-8")
    return check(tmp_path)[1]


def test_adopt_needs_a_task_store_whose_quality_rules_require_the_key(tmp_path):
    assert issues_of(tmp_path, CONFIG) == []
    for bad in (CONFIG.replace("require: [link, due, est, area, key]", "require: [link, due, est, area]"),
                CONFIG.replace("optional: [due]", "optional: [due, key]"),
                CONFIG.split("    quality:\n")[0]):
        [issue] = issues_of(tmp_path, bad)
        assert issue.code == "config.adopt_needs_key" and issue.path == "destinations.backlog.adopt"
        assert "quality.require" in issue.message and "key" in issue.message
    other = CONFIG + "  cal:\n    adapter: calendar_file\n    path: cal.ics\n    adopt: true\n"
    [issue] = issues_of(tmp_path, other)
    assert issue.code == "config.capability_missing" and issue.path == "destinations.cal.adopt"
    assert "markdown_tasks" in issue.message


def test_one_plan_node_per_graph_adopts(tmp_path):
    """The first plan node that is approved and applied and schedules tasks; else the first
    that is approved and applied. A plan nobody applies writes nothing, so it adopts nothing."""
    from types import SimpleNamespace

    from docket.config.models import Node, ordered
    ws = workspace(tmp_path)

    def rc(graph: dict[str, dict]) -> SimpleNamespace:
        nodes = {k: Node.model_validate(v) for k, v in graph.items()}
        order = ordered(nodes)
        return SimpleNamespace(ws=ws, graph=nodes,
                               by_verb=lambda verb: [n for n in order if nodes[n].verb == verb])

    two = {"decide": {"ask": "default"}, "plan-a": {"plan": ["decide"]},
           "plan-b": {"plan": ["decide"], "schedule_tasks": True},
           "approve-a": {"approve": "plan-a"}, "apply-a": {"apply": "approve-a"},
           "approve-b": {"approve": "plan-b"}, "apply-b": {"apply": "approve-b"}}
    assert adopt.node(rc(two)) == "plan-b"
    two["plan-b"].pop("schedule_tasks")
    assert adopt.node(rc(two)) == "plan-a"
    del two["apply-a"]
    assert adopt.node(rc(two)) == "plan-b"
    assert adopt.node(rc({"decide": {"ask": "default"}, "plan": {"plan": ["decide"]}})) is None
    off = workspace(tmp_path, CONFIG.replace("    adopt: true\n", ""))
    graph = rc(two)
    graph.ws = off
    assert adopt.node(graph) is None
