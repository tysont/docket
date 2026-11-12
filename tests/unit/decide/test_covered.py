"""Covered questions (TD §10.4, §12.8): for the latest meeting of a forum that left no record,
the operator says which agenda entries were covered. The answers of one meeting become one
roll of its agenda. All names and data are invented."""
from __future__ import annotations

import pytest

from docket.config.models import Destination as DestConfig
from docket.config.models import Forum, Profile
from docket.decide import covered
from docket.destinations import build
from docket.destinations.base import DestEnv, Operation

pytestmark = pytest.mark.smoke

NOTE = """# 1-1 · Sam

## Next

- Hiring plan
  - two open roles
- Launch date
- Budget

## 2026-10-01

- Earlier topic
"""
FORUMS = [Forum(id="sam", name="Sam", kind="person", agenda={"destination": "agendas", "path": "meet/sam.md"},
                calendar_match={"attendee": "sam@example.org"}),
          Forum(id="weekly", name="Weekly", agenda={"destination": "agendas", "path": "meet/weekly.md"},
                calendar_match={"title_regex": "^Platform Sync"}),
          Forum(id="quiet", name="Quiet", calendar_match={"title_regex": "^Quiet"})]   # no agenda
PROFILE = Profile.model_validate({"me": {"name": "Operator", "handles": {"mail": "me@example.org"}},
                                  "forums": [f.model_dump() for f in FORUMS]})
ME = {"me@example.org"}


def dest(tmp_path, sam=NOTE, weekly="# Weekly\n\n## Next\n\n- Roadmap\n"):
    (tmp_path / "meet").mkdir(exist_ok=True)
    (tmp_path / "meet" / "sam.md").write_text(sam)
    (tmp_path / "meet" / "weekly.md").write_text(weekly)
    cfg = DestConfig.model_validate({"adapter": "markdown_agenda", "path": "meet", "section": "## Next"})
    return build("agendas", cfg, DestEnv(root=tmp_path, profile=PROFILE, sandbox_dir=tmp_path / "sbx"))


def meeting(i, date, title="Operator / Sam", attendees=("me@example.org", "sam@example.org"), record=False):
    return {"id": i, "date": date, "title": title, "attendees": list(attendees), "record": record}


def rows_for(d, meetings, skip=()):
    return covered.rows(d, d.snapshot(), FORUMS, ME, meetings, to="agendas", suffix="no record", skip=set(skip))


def test_one_question_per_entry_for_the_latest_unrecorded_meeting_of_each_forum(tmp_path):
    d = dest(tmp_path)
    rows = rows_for(d, [
        meeting("m1", "2026-10-08"),                                        # passed over: a later one follows
        meeting("m2", "2026-10-15"),
        meeting("w1", "2026-10-14", title="Platform Sync", attendees=("me@example.org", "a@example.org", "b@example.org")),
        meeting("q1", "2026-10-14", title="Quiet hour"),                    # its forum has no agenda
        meeting("x1", "2026-10-14", title="Lunch", attendees=("me@example.org", "kim@example.org")),   # no forum
    ])
    assert [(r["meeting"]["forum"], r["meeting"]["id"], r["meeting"]["date"], r["entry"]["text"]) for r in rows] == [
        ("sam", "m2", "2026-10-15", "- Hiring plan"), ("sam", "m2", "2026-10-15", "- Launch date"),
        ("sam", "m2", "2026-10-15", "- Budget"), ("weekly", "w1", "2026-10-14", "- Roadmap")]
    assert rows[0]["entry"]["block"] == ["- Hiring plan", "  - two open roles"]
    assert rows[0]["meeting"]["forum_name"] == "Sam" and rows[0]["meeting"]["path"] == "meet/sam.md"


@pytest.mark.parametrize("meetings,why", [
    ([meeting("m1", "2026-10-15", record=True)], "the meeting has a record: the recap rolls it"),
    ([meeting("m1", "2026-10-08"), meeting("m2", "2026-10-15", record=True)], "the latest meeting has a record"),
    ([meeting("m1", "2026-10-01")], "not newer than the latest dated section"),
    ([meeting("m1", "2026-09-24")], "older than the latest dated section"),
    ([], "no meeting"),
])
def test_no_question_without_an_unrecorded_latest_meeting(tmp_path, meetings, why):
    d = dest(tmp_path)
    assert [r for r in rows_for(d, meetings) if r["meeting"]["forum"] == "sam"] == [], why


def test_no_question_when_the_agenda_has_no_entries(tmp_path):
    d = dest(tmp_path, sam=NOTE.replace("- Hiring plan\n  - two open roles\n- Launch date\n- Budget\n", ""))
    assert rows_for(d, [meeting("m1", "2026-10-15")]) == []


def test_question_ids_are_stable_per_meeting_and_entry_and_follow_the_entry_text(tmp_path):
    d = dest(tmp_path)
    one = [covered.question_id(r) for r in rows_for(d, [meeting("m1", "2026-10-15")])]
    again = [covered.question_id(r) for r in rows_for(d, [meeting("m1-again", "2026-10-15")])]
    later = [covered.question_id(r) for r in rows_for(d, [meeting("m3", "2026-10-22")])]
    assert one == again and len(set(one)) == 3 and all(q.startswith("c_") for q in one)
    assert not set(one) & set(later)                                        # the next meeting asks afresh
    # an entry remembered as kept for this meeting is not asked again
    assert [covered.question_id(r) for r in rows_for(d, [meeting("m1", "2026-10-15")], skip=one[:1])] == one[1:]
    d2 = dest(tmp_path, sam=NOTE.replace("- Budget", "- Budget for next year"))
    edited = [covered.question_id(r) for r in rows_for(d2, [meeting("m1", "2026-10-15")])]
    assert edited[:2] == one[:2] and edited[2] != one[2]


def answered(rows, *options):
    return {covered.question_id(r): {"option": o} for r, o in zip(rows, options)}


def apply_all(d, ops):
    for o in ops:
        assert d.apply(Operation(**o)).outcome == "applied"


def test_covered_kept_and_dropped_entries_become_one_roll(tmp_path):
    d = dest(tmp_path)
    rows = rows_for(d, [meeting("m1", "2026-10-15")])
    ops = covered.meeting_operations(d, d.snapshot(), rows, answered(rows, "covered", "keep", "drop"))
    [op] = ops
    assert op["verb"] == "roll" and op["title"] == "## 2026-10-15 *(no record)*"
    assert op["body"] == "- Hiring plan\n  - two open roles"
    assert op["remove"] == ["- Hiring plan"] and op["drop"] == ["- Budget"]
    apply_all(d, ops)
    assert (tmp_path / "meet" / "sam.md").read_text() == """# 1-1 · Sam

## Next

- Launch date

## 2026-10-15 *(no record)*

- Hiring plan
  - two open roles

## 2026-10-01

- Earlier topic
"""
    assert d.verify(Operation(**op), None).ok
    # the meeting now has its section: it is not asked about again
    assert rows_for(d, [meeting("m1", "2026-10-15")]) == []


def test_everything_covered_moves_the_whole_agenda(tmp_path):
    d = dest(tmp_path)
    rows = rows_for(d, [meeting("m1", "2026-10-15")])
    apply_all(d, covered.meeting_operations(d, d.snapshot(), rows, answered(rows, "covered", "covered", "covered")))
    assert (tmp_path / "meet" / "sam.md").read_text() == """# 1-1 · Sam

## Next

## 2026-10-15 *(no record)*

- Hiring plan
  - two open roles
- Launch date
- Budget

## 2026-10-01

- Earlier topic
"""


def test_dropped_entries_with_nothing_covered_are_only_removed(tmp_path):
    d = dest(tmp_path)
    rows = rows_for(d, [meeting("m1", "2026-10-15")])
    ops = covered.meeting_operations(d, d.snapshot(), rows, answered(rows, "keep", "drop", "keep"))
    [op] = ops
    assert op["verb"] == "roll" and not op.get("title") and not op.get("body") and op["drop"] == ["- Launch date"]
    apply_all(d, ops)
    assert (tmp_path / "meet" / "sam.md").read_text() == NOTE.replace("- Launch date\n", "")
    assert d.verify(Operation(**op), None).ok
    assert d.apply(Operation(**op)).outcome in ("applied", "already_present")   # repeating changes nothing
    assert (tmp_path / "meet" / "sam.md").read_text() == NOTE.replace("- Launch date\n", "")


def test_everything_kept_is_no_operation(tmp_path):
    d = dest(tmp_path)
    rows = rows_for(d, [meeting("m1", "2026-10-15")])
    assert covered.meeting_operations(d, d.snapshot(), rows, answered(rows, "keep", "keep", "keep")) == []
    assert (tmp_path / "meet" / "sam.md").read_text() == NOTE


def test_two_forums_get_one_roll_each(tmp_path):
    d = dest(tmp_path)
    rows = rows_for(d, [meeting("m1", "2026-10-15"), meeting(
        "w1", "2026-10-14", title="Platform Sync", attendees=("me@example.org", "a@example.org", "b@example.org"))])
    ops = covered.meeting_operations(d, d.snapshot(), rows, answered(rows, "keep", "covered", "keep", "covered"))
    assert [(o["path"], o["title"]) for o in ops] == [("meet/sam.md", "## 2026-10-15 *(no record)*"),
                                                      ("meet/weekly.md", "## 2026-10-14 *(no record)*")]
    apply_all(d, ops)
    assert (tmp_path / "meet" / "weekly.md").read_text() == "# Weekly\n\n## Next\n\n## 2026-10-14 *(no record)*\n\n- Roadmap\n"
