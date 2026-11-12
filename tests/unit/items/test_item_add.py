"""``docket item add``: a task the operator decided on without being asked. Its topic comes
from its words and its first link, so adding it again finds it; its action carries the
fields a triaged item's action carries, so a workspace's line template renders it the same
way (TD §13.8)."""
from __future__ import annotations

from pathlib import Path

import pytest

from docket.config import load_workspace
from docket.config.contexts import TASK_LINE
from docket.items import add, ledger

pytestmark = pytest.mark.smoke

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
    sections: { inbox: "## Inbox", priority: "## Priority" }
"""


def workspace(tmp_path: Path, extra: str = "") -> object:
    (tmp_path / "docket.yaml").write_text(CONFIG + extra, encoding="utf-8")
    return load_workspace(tmp_path)


def test_the_topic_is_the_words_and_the_first_link_and_nothing_else():
    t = add.topic_of("Watch the storage talk", "https://video.example.org/v/1")
    assert t.startswith("hand:") and t == add.topic_of("  watch the   Storage talk ", "https://video.example.org/v/1")
    assert t != add.topic_of("Watch the storage talk", None)
    assert t != add.topic_of("Watch the storage talk", "https://video.example.org/v/2")
    assert add.topic_of("Call the dentist", None) == add.topic_of("Call the dentist", "")
    assert ledger.item_key(t) == add.key_of("Watch the storage talk", "https://video.example.org/v/1")
    assert len(add.key_of("Call the dentist", None)) == 9 and add.key_of("Call the dentist", None).startswith("i")


def test_the_action_carries_the_fields_a_triaged_items_action_carries(tmp_path):
    ws = workspace(tmp_path)
    ctx = add.action_fields(ws, text="Watch the storage talk", links=["https://video.example.org/v/1",
                                                                      "https://notes.example.org/n/2"],
                            label="video", due="2026-10-20", est=45, area="Learning", tier="priority",
                            note="recommended in the team sync", today="2026-10-15", key="iabcdefgh",
                            topic="hand:x")
    assert ctx["action"] == ctx["title"] == "Watch the storage talk"
    assert ctx["url"] == "https://video.example.org/v/1"
    assert ctx["links"] == ["https://video.example.org/v/1", "https://notes.example.org/n/2"]
    assert ctx["source_label"] == "video" and ctx["source"] == "hand"
    assert (ctx["due"], ctx["est"], ctx["area"], ctx["tier"]) == ("2026-10-20", 45, "Learning", "priority")
    assert ctx["note"] == "recommended in the team sync" and ctx["key"] == "iabcdefgh" and ctx["topic"] == "hand:x"
    assert ctx["run"]["date"] == "2026-10-15" and ctx["run"]["workdays"] == [0, 1, 2, 3, 4]
    assert ctx["from"] == "" and ctx["quote"] == ""                           # no sender, no quote
    assert ctx["text"] == "Watch the storage talk"                             # the operator's own words are the evidence
    # every field a task line template may name is there, so any valid template renders
    roots = {n.split(".")[0].split("[")[0] for n in TASK_LINE.paths}
    assert roots <= set(ctx), sorted(roots - set(ctx))


def test_an_action_with_nothing_optional_leaves_those_fields_empty(tmp_path):
    ws = workspace(tmp_path)
    ctx = add.action_fields(ws, text="Call the dentist", links=[], label="link", due=None, est=None, area=None,
                            tier=None, note=None, today="2026-10-15", key="iabcdefgh", topic="hand:y")
    assert (ctx["url"], ctx["links"], ctx["due"], ctx["est"], ctx["area"], ctx["note"]) == ("", [], "", "", "", "")
    assert "tier" not in ctx            # no tier: the destination's default section


def test_task_stores_are_the_destinations_that_accept_task_add(tmp_path):
    ws = workspace(tmp_path, "  agendas: { adapter: markdown_agenda, path: Syncs }\n"
                             "  later: { adapter: markdown_tasks, path: later.md, visibility: shared }\n")
    assert add.task_stores(ws) == ["backlog", "later"]
