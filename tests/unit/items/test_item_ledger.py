"""The item ledger: one row for every item Docket has put in front of the operator, with a
short stable key, its state, every decision made on it and where it now lives. A state is
where an item is; a decision is what moves it. Destinations are projections of the ledger
and carry the key."""
from __future__ import annotations

import sqlite3

import pytest

from docket.items import ledger
from docket.store.db import open_db

pytestmark = pytest.mark.smoke
T1, T2 = "2026-10-15T18:00:00+00:00", "2026-10-16T18:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def item(i, topic, title="Invented item", url="https://chat.example.org/t/1", source="chat"):
    return {"id": i, "topic": topic, "title": title, "url": url, "source": source}


def test_the_key_is_short_stable_and_of_the_topic_not_the_run():
    k = ledger.item_key("thread:chat:room-1/threads/t1")
    assert k == ledger.item_key("thread:chat:room-1/threads/t1") and k != ledger.item_key("thread:chat:room-1/threads/t2")
    assert len(k) == 9 and k[0] == "i" and k.isalnum() and k == k.lower()


def test_states_follow_decisions_and_the_history_keeps_every_one(db):
    a, b, c = item("a", "topic-a"), item("b", "topic-b"), item("c", "topic-c")
    rows = ledger.derive("r1", T1, kept=[a, b, c], decided=[
        {"topic": "topic-a", "decision": "routed", "destination": "backlog", "adapter": "markdown_tasks",
         "op_id": "op_1", "path": "tasks.md", "anchor": "## Inbox", "due": "2026-10-20", "est": 30, "area": "Platform"},
        {"topic": "topic-b", "decision": "ack"}])
    with db:
        ledger.write(db, rows)
    got = {r["topic"]: r for r in ledger.rows(db)}
    assert (got["topic-a"]["state"], got["topic-b"]["state"], got["topic-c"]["state"]) == ("backlog", "acknowledged", "new")
    assert got["topic-a"]["key"] == ledger.item_key("topic-a") and got["topic-a"]["url"] == a["url"]
    assert got["topic-a"]["placements"] == [{"destination": "backlog", "adapter": "markdown_tasks", "op_id": "op_1",
                                             "path": "tasks.md", "anchor": "## Inbox", "run": "r1"}]
    assert (got["topic-a"]["due"], got["topic-a"]["est"], got["topic-a"]["area"]) == ("2026-10-20", 30, "Platform")
    assert got["topic-c"]["history"] == [] and got["topic-c"]["first_run"] == "r1"

    # the next run: the parked item gets a calendar block, the acknowledged one is acted on
    rows = ledger.derive("r2", T2, kept=[a, b], decided=[
        {"topic": "topic-a", "decision": "routed", "destination": "work-calendar", "adapter": "calendar_exec",
         "op_id": "op_2", "key": "blk_9"},
        {"topic": "topic-b", "decision": "now", "note": "replied"}])
    with db:
        ledger.write(db, rows)
    got = {r["topic"]: r for r in ledger.rows(db)}
    assert got["topic-a"]["state"] == "scheduled" and got["topic-a"]["first_run"] == "r1" and got["topic-a"]["last_run"] == "r2"
    assert [p["destination"] for p in got["topic-a"]["placements"]] == ["backlog", "work-calendar"]
    assert [(h["run"], h["decision"], h["state"]) for h in got["topic-a"]["history"]] == [
        ("r1", "routed", "backlog"), ("r2", "routed", "scheduled")]
    assert got["topic-b"]["state"] == "done" and got["topic-b"]["history"][-1]["note"] == "replied"
    assert got["topic-c"]["state"] == "new" and got["topic-c"]["last_run"] == "r1"      # untouched by r2
    assert [r["topic"] for r in ledger.rows(db, state="scheduled")] == ["topic-a"]
    assert ledger.get(db, got["topic-a"]["key"])["topic"] == "topic-a" and ledger.get(db, "inope") is None


@pytest.mark.parametrize("decision,adapter,state", [
    ("routed", "markdown_tasks", "backlog"), ("routed", "calendar_exec", "scheduled"),
    ("routed", "calendar_file", "scheduled"), ("routed", "markdown_agenda", "raised"),
    ("watch", None, "monitoring"), ("ack", None, "acknowledged"), ("now", None, "done"), ("done", None, "done"),
    ("dismiss", None, "ignored"),
])
def test_each_decision_has_one_state(decision, adapter, state):
    assert ledger.state_of(decision, adapter) == state


def test_an_item_seen_again_and_not_decided_keeps_its_state(db):
    a = item("a", "topic-a")
    with db:
        ledger.write(db, ledger.derive("r1", T1, kept=[a], decided=[{"topic": "topic-a", "decision": "watch"}]))
        ledger.write(db, ledger.derive("r2", T2, kept=[a], decided=[]))
    (row,) = ledger.rows(db)
    assert row["state"] == "monitoring" and row["last_run"] == "r2" and len(row["history"]) == 1


def test_items_can_be_imported_by_hand(db):
    """Items from before the ledger, or from anywhere else, enter it by import: a title, and
    whatever else is known. An import never overwrites a row the ledger already has."""
    with db:
        report = ledger.import_rows(db, [
            {"title": "Draft the storage plan", "url": "https://notes.example.org/plan", "due": "2026-11-01", "est": "90m",
             "area": "Platform", "note": "from the old list"},
            {"title": "Call the vendor", "state": "monitoring"},
            {"title": ""}, {"title": "Odd state", "state": "pending"}], run_id="import-1", at=T1)
    assert report["imported"] == 2 and [p["row"] for p in report["problems"]] == [2, 3]
    got = {r["title"]: r for r in ledger.rows(db)}
    plan = got["Draft the storage plan"]
    assert plan["state"] == "backlog" and plan["est"] == 90 and plan["source"] == "import" and plan["first_run"] == "import-1"
    assert plan["history"][0]["note"] == "from the old list" and got["Call the vendor"]["state"] == "monitoring"
    with db:
        again = ledger.import_rows(db, [{"title": "Draft the storage plan", "url": "https://notes.example.org/plan",
                                         "state": "done"}], run_id="import-2", at=T2)
    assert again["imported"] == 0 and again["skipped"] == 1
    assert ledger.get(db, plan["key"])["state"] == "backlog"


def test_the_ledger_follows_what_happened_in_a_destination(db):
    """The operator works in the destination, not in Docket: a task is ticked there, or
    removed. The ledger reads each placement back through its destination and follows."""
    a, b, c, d = (item(x, f"topic-{x}") for x in "abcd")
    routed = lambda t: {"topic": t, "decision": "routed", "destination": "backlog", "adapter": "markdown_tasks",  # noqa: E731
                        "op_id": "op_" + t}
    with db:
        ledger.write(db, ledger.derive("r1", T1, kept=[a, b, c, d], decided=[
            routed("topic-a"), routed("topic-b"), routed("topic-c"), {"topic": "topic-d", "decision": "watch"}]))
    key = {t: ledger.item_key(f"topic-{t}") for t in "abcd"}
    with db:   # at the seal every line is there and open; a destination that never wrote a key says nothing
        assert ledger.observe(db, {"backlog": {key["a"]: "open", key["c"]: "open"}}, run_id="r1", at=T1) == []
    assert ledger.get(db, key["b"])["state"] == "backlog"
    seen = {"backlog": {key["a"]: "open", key["b"]: "done"}}          # c is no longer there
    moves = ledger.observe(db, seen, run_id="r2", at=T2, write=False)
    assert {(m["key"], m["state"]) for m in moves} == {(key["b"], "done"), (key["c"], "done")}
    assert ledger.get(db, key["b"])["state"] == "backlog"              # a preview writes nothing
    with db:
        ledger.observe(db, seen, run_id="r2", at=T2)
    got = {t: ledger.get(db, key[t]) for t in "abcd"}
    assert [got[t]["state"] for t in "abcd"] == ["backlog", "done", "done", "monitoring"]
    assert got["b"]["history"][-1] == {"run": "r2", "at": T2, "decision": "observed", "state": "done",
                                       "note": "closed in backlog"}
    assert got["c"]["history"][-1]["note"] == "no longer in backlog"
    with db:
        assert ledger.observe(db, seen, run_id="r3", at=T2) == []       # settled: nothing moves twice


def test_a_destination_that_could_not_be_read_moves_nothing(db):
    """An unreadable or empty destination is not evidence that every task was removed."""
    with db:
        ledger.write(db, ledger.derive("r1", T1, kept=[item("a", "topic-a")], decided=[
            {"topic": "topic-a", "decision": "routed", "destination": "backlog", "adapter": "markdown_tasks", "op_id": "o"}]))
        assert ledger.observe(db, {}, run_id="r2", at=T2) == []
        assert ledger.observe(db, {"backlog": None}, run_id="r2", at=T2) == []
    assert ledger.rows(db)[0]["state"] == "backlog"


def test_an_imported_item_can_say_where_it_lives_and_is_followed_there(db):
    """An item imported from a list that already exists is in that list. Naming the
    destination and the key written on its line lets the ledger follow it like any other."""
    with db:
        report = ledger.import_rows(db, [
            {"title": "Draft the storage plan", "key": "iaaaaaaaa", "destination": "backlog", "adapter": "markdown_tasks"},
            {"title": "Bad key", "key": "nope"},
            {"title": "Half a place", "destination": "backlog"}], run_id="import-1", at=T1)
    assert report["imported"] == 1 and [p["row"] for p in report["problems"]] == [1, 2]
    row = ledger.get(db, "iaaaaaaaa")
    assert row["title"] == "Draft the storage plan" and row["state"] == "backlog"
    assert row["placements"] == [{"destination": "backlog", "adapter": "markdown_tasks", "run": "import-1"}]
    with db:
        assert ledger.observe(db, {"backlog": {"iaaaaaaaa": "open"}}, run_id="r2", at=T2) == []
        moves = ledger.observe(db, {"backlog": {"iaaaaaaaa": "done"}}, run_id="r3", at=T2)
    assert [(m["key"], m["state"]) for m in moves] == [("iaaaaaaaa", "done")]


def _backlog_item(db, key, title="A task"):
    ledger.import_rows(db, [{"title": title, "key": key, "destination": "backlog", "adapter": "markdown_tasks"}],
                       run_id="import-1", at=T1)


def test_blocks_are_tracked_back_to_their_items_whether_whole_batched_or_split(db):
    """A block on the calendar is a placement of the items it is for. One item may have
    several blocks (a split task), and one block several items (a batch); the ledger holds
    both directions, and an item with a block is scheduled."""
    with db:
        for k in ("iaaaaaaaa", "ibbbbbbbb", "icccccccc", "idddddddd"):
            _backlog_item(db, k)
        blocks = [
            {"key": "dk_t_whole", "task_ids": ["iaaaaaaaa"], "start": "2026-10-16T09:00", "end": "2026-10-16T10:00"},
            {"key": "dk_t_part1", "task_ids": ["ibbbbbbbb"], "start": "2026-10-16T10:00", "end": "2026-10-16T11:00"},
            {"key": "dk_t_part2", "task_ids": ["ibbbbbbbb"], "start": "2026-10-19T09:00", "end": "2026-10-19T10:00"},
            {"key": "dk_b_batch", "task_ids": ["icccccccc", "idddddddd", "t_not_in_the_ledger"],
             "start": "2026-10-16T13:00", "end": "2026-10-16T13:30"}]
        moves = ledger.place_blocks(db, blocks, destination="cal", adapter="calendar_exec", run_id="r2", at=T2)
    assert sorted(m["key"] for m in moves) == ["iaaaaaaaa", "ibbbbbbbb", "icccccccc", "idddddddd"]
    got = {k: ledger.get(db, k) for k in ("iaaaaaaaa", "ibbbbbbbb", "icccccccc")}
    assert all(r["state"] == "scheduled" for r in got.values())
    cal = lambda r: [p for p in r["placements"] if p["adapter"] == "calendar_exec"]      # noqa: E731
    assert [p["key"] for p in cal(got["ibbbbbbbb"])] == ["dk_t_part1", "dk_t_part2"]
    assert cal(got["ibbbbbbbb"])[0] == {"destination": "cal", "adapter": "calendar_exec", "key": "dk_t_part1",
                                        "start": "2026-10-16T10:00", "end": "2026-10-16T11:00", "of": 2, "run": "r2"}
    assert cal(got["icccccccc"])[0]["with"] == ["idddddddd", "t_not_in_the_ledger"]      # the batch it shares
    assert got["iaaaaaaaa"]["placements"][0]["adapter"] == "markdown_tasks"              # its line is still its home
    assert [r["key"] for r in ledger.items_of_block(db, "dk_b_batch")] == ["icccccccc", "idddddddd"]
    assert [r["key"] for r in ledger.items_of_block(db, "dk_t_part2")] == ["ibbbbbbbb"]


def test_tracking_follows_when_blocks_are_consolidated_split_up_or_released(db):
    with db:
        for k in ("iaaaaaaaa", "ibbbbbbbb"):
            _backlog_item(db, k)
        two = [{"key": "dk_t_p1", "task_ids": ["iaaaaaaaa"], "start": "2026-10-16T09:00", "end": "2026-10-16T10:00"},
               {"key": "dk_t_p2", "task_ids": ["iaaaaaaaa"], "start": "2026-10-19T09:00", "end": "2026-10-19T10:00"},
               {"key": "dk_b_x", "task_ids": ["ibbbbbbbb"], "start": "2026-10-16T13:00", "end": "2026-10-16T13:30"}]
        ledger.place_blocks(db, two, destination="cal", adapter="calendar_exec", run_id="r2", at=T2)
        # the next plan: the split task is one block again, and the batched one has no block
        one = [{"key": "dk_t_whole", "task_ids": ["iaaaaaaaa"], "start": "2026-10-20T09:00", "end": "2026-10-20T11:00"}]
        moves = ledger.place_blocks(db, one, destination="cal", adapter="calendar_exec", run_id="r3", at=T2)
        assert ledger.place_blocks(db, one, destination="cal", adapter="calendar_exec", run_id="r4", at=T2) == []
    a, b = ledger.get(db, "iaaaaaaaa"), ledger.get(db, "ibbbbbbbb")
    assert [p["key"] for p in a["placements"] if p["adapter"] == "calendar_exec"] == ["dk_t_whole"] and a["state"] == "scheduled"
    assert [p for p in b["placements"] if p["adapter"] == "calendar_exec"] == [] and b["state"] == "backlog"
    assert b["history"][-1]["decision"] == "planned" and b["history"][-1]["note"] == "no block"
    assert {m["key"]: m["state"] for m in moves} == {"iaaaaaaaa": "scheduled", "ibbbbbbbb": "backlog"}
    assert ledger.items_of_block(db, "dk_t_p1") == []


def test_a_scheduled_item_is_still_followed_in_its_task_list(db):
    with db:
        _backlog_item(db, "iaaaaaaaa")
        ledger.observe(db, {"backlog": {"iaaaaaaaa": "open"}}, run_id="r1", at=T1)
        ledger.place_blocks(db, [{"key": "dk_t_w", "task_ids": ["iaaaaaaaa"], "start": "s", "end": "e"}],
                            destination="cal", adapter="calendar_exec", run_id="r2", at=T2)
        moves = ledger.observe(db, {"backlog": {"iaaaaaaaa": "done"}}, run_id="r3", at=T2)
    assert [(m["key"], m["state"]) for m in moves] == [("iaaaaaaaa", "done")]
    with db:   # a finished item is not pulled back by a block that still stands
        assert ledger.place_blocks(db, [{"key": "dk_t_w", "task_ids": ["iaaaaaaaa"], "start": "s", "end": "e"}],
                                   destination="cal", adapter="calendar_exec", run_id="r4", at=T2) == []
    assert ledger.get(db, "iaaaaaaaa")["state"] == "done"


def test_an_item_raised_in_a_meeting_is_done_when_the_meeting_covers_it(db):
    """An item put on an agenda is raised. When the agenda is rolled and the entry leaves it,
    the meeting dealt with it: the item is done, and its history says where and when. The
    entry is recognised by its line as written, or by the item's link if the line was edited."""
    a, b, c = item("a", "topic-a", url="https://chat.example.org/t/a"), item("b", "topic-b", url="https://chat.example.org/t/b"), \
        item("c", "topic-c", url="https://chat.example.org/t/c")
    place = lambda t, line: {"topic": t, "decision": "routed", "destination": "agendas", "adapter": "markdown_agenda",  # noqa: E731
                             "op_id": "op_" + t, "path": "meet/sam.md", "anchor": "## Next", "line": line}
    with db:
        ledger.write(db, ledger.derive("r1", T1, kept=[a, b, c], decided=[
            place("topic-a", "- Capacity plan - who signs off? [chat](https://chat.example.org/t/a)"),
            place("topic-b", "- Hiring - two reqs or one? [chat](https://chat.example.org/t/b)"),
            place("topic-c", "- Offsite - when? [chat](https://chat.example.org/t/c)")]))
    assert {r["state"] for r in ledger.rows(db)} == {"raised"}
    assert ledger.rows(db)[0]["placements"][0]["line"].startswith("- Capacity plan")
    with db:
        moves = ledger.cover(db, [
            {"path": "meet/sam.md", "title": "## 2026-10-15",
             "remove": ["- Capacity plan - who signs off? [chat](https://chat.example.org/t/a)",
                        "- Hiring, reworded by hand [chat](https://chat.example.org/t/b)", "- Something never tracked"]},
            {"path": "meet/other.md", "title": "## 2026-10-15", "remove": ["- Offsite - when? [chat](https://chat.example.org/t/c)"]},
        ], run_id="r2", at=T2)
    got = {r["topic"]: r for r in ledger.rows(db)}
    assert sorted(m["key"] for m in moves) == sorted([got["topic-a"]["key"], got["topic-b"]["key"]])
    assert got["topic-a"]["state"] == "done" and got["topic-b"]["state"] == "done"
    assert got["topic-a"]["history"][-1] == {"run": "r2", "at": T2, "decision": "covered", "state": "done",
                                             "note": "covered 2026-10-15 in meet/sam.md"}
    assert got["topic-c"]["state"] == "raised"                       # another agenda's roll says nothing of it
    with db:
        assert ledger.cover(db, [{"path": "meet/sam.md", "title": "## 2026-10-15", "remove": ["- x"]}], run_id="r3", at=T2) == []


def test_an_item_is_edited_by_hand_and_only_what_is_given_changes(db):
    """A hand edit changes the ledger row, not the destinations. Each edit is a history entry
    that says it was made by hand, with the note and what changed."""
    with db:
        _backlog_item(db, "iaaaaaaaa", "Draft the plan")
        ledger.place_blocks(db, [{"key": "dk_t_w", "task_ids": ["iaaaaaaaa"], "start": "s", "end": "e"}],
                            destination="cal", adapter="calendar_exec", run_id="r2", at=T1)
        res = ledger.edit(db, "iaaaaaaaa", {"state": "backlog", "due": "2026-10-23", "est": 90, "area": None},
                          note="moved after the review", at=T2)
    assert res["changes"] == {"state": ["scheduled", "backlog"], "due": [None, "2026-10-23"], "est": [None, 90]}
    row = ledger.get(db, "iaaaaaaaa")
    assert (row["state"], row["state_at"], row["due"], row["est"], row["title"]) == \
        ("backlog", T2, "2026-10-23", 90, "Draft the plan")
    assert row["history"][-1] == {"at": T2, "by": "hand", "decision": "edited", "state": "backlog",
                                  "note": "moved after the review",
                                  "changes": {"state": ["scheduled", "backlog"], "due": [None, "2026-10-23"],
                                              "est": [None, 90]}}
    assert row["placements"] == res["placements"] and len(row["placements"]) == 2      # untouched
    with db:
        res = ledger.edit(db, "iaaaaaaaa", {"title": "Draft the plan v2"}, note=None, at=T2)
    assert res["changes"] == {"title": ["Draft the plan", "Draft the plan v2"]}
    row = ledger.get(db, "iaaaaaaaa")
    assert row["state_at"] == T2 and "note" not in row["history"][-1] and row["history"][-1]["state"] == "backlog"


def test_an_edit_that_changes_nothing_writes_nothing(db):
    with db:
        _backlog_item(db, "iaaaaaaaa")
        before = ledger.get(db, "iaaaaaaaa")
        res = ledger.edit(db, "iaaaaaaaa", {"state": "backlog", "due": None}, note="x", at=T2)
        dry = ledger.edit(db, "iaaaaaaaa", {"state": "done"}, note=None, at=T2, write=False)
    assert res["changes"] == {} and ledger.get(db, "iaaaaaaaa") == before
    assert dry["changes"] == {"state": ["backlog", "done"]} and ledger.get(db, "iaaaaaaaa") == before
    assert ledger.edit(db, "inope", {"state": "done"}, note=None, at=T2) is None
