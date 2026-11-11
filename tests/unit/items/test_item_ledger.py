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
    with db:   # at close every line is there and open; a destination that never wrote a key says nothing
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
