"""Rolling agendas after meetings (TD §12.8): for each forum, the meetings that happened
since its agenda was last rolled become dated sections, oldest first."""
from __future__ import annotations

import pytest

from docket.config.models import Destination as DestConfig
from docket.config.models import Forum, Profile
from docket.destinations import build
from docket.destinations.base import DestEnv, Operation
from docket.plan.agenda_roll import forum_for_meeting, roll_operations

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
FORUMS = [Forum(id="sam", kind="person", agenda={"path": "meet/sam.md"}, calendar_match={"attendee": "sam@example.org"}),
          Forum(id="weekly", agenda={"path": "meet/weekly.md"}, calendar_match={"title_regex": "^Platform Sync"})]
PROFILE = Profile.model_validate({"me": {"name": "Operator", "handles": {"mail": "me@example.org"}},
                                  "forums": [f.model_dump() for f in FORUMS]})


def dest(tmp_path, sam=NOTE):
    (tmp_path / "meet").mkdir(exist_ok=True)
    (tmp_path / "meet" / "sam.md").write_text(sam)
    (tmp_path / "meet" / "weekly.md").write_text("# Weekly\n\n## Next\n\n- Roadmap\n")
    cfg = DestConfig.model_validate({"adapter": "markdown_agenda", "path": "meet", "section": "## Next"})
    return build("agendas", cfg, DestEnv(root=tmp_path, profile=PROFILE, sandbox_dir=tmp_path / "sbx"))


def meeting(i, date, title="Operator / Sam", attendees=("me@example.org", "sam@example.org"), recap=None, url=None):
    return {"id": i, "date": date, "title": title, "attendees": list(attendees), "url": url, "recap": recap}


def apply_all(d, ops):
    for o in ops:
        assert d.apply(Operation(**o)).outcome == "applied"


def test_a_meeting_finds_its_forum_by_who_was_there_or_by_its_title():
    me = {"me@example.org"}
    assert forum_for_meeting(FORUMS, "Anything at all", ["me@example.org", "Sam@Example.org"], me).id == "sam"
    assert forum_for_meeting(FORUMS, "Platform Sync (weekly)", ["me@example.org", "ann@example.org", "bo@example.org"], me).id == "weekly"
    assert forum_for_meeting(FORUMS, "Operator / Sam", ["me@example.org", "sam@example.org", "kim@example.org"], me) is None
    assert forum_for_meeting(FORUMS, "Lunch", ["me@example.org", "kim@example.org"], me) is None


def test_each_meeting_since_the_last_roll_gets_its_section_oldest_first(tmp_path):
    """Two meetings with a record and then one without. Each recorded meeting keeps what it
    covered and takes the agenda entries it settled; the last, with no record, takes what
    is still on the agenda. Nothing is rolled twice."""
    d = dest(tmp_path)
    ops = roll_operations(d, d.snapshot(), FORUMS, {"me@example.org"}, [
        meeting("m3", "2026-10-22"),
        meeting("m2", "2026-10-15", url="https://docs.example.org/n2",
                recap={"covered": [{"text": "Launch moves to November", "next": "N2"}], "actions": []}),
        meeting("m1", "2026-10-08", url="https://docs.example.org/n1",
                recap={"covered": [{"text": "Hiring: two offers out", "next": "N1"}, {"text": "Reorg timing"}],
                       "actions": ["Send the offer letters"]}),
        meeting("m0", "2026-09-24", recap={"covered": [{"text": "Older than the last roll"}], "actions": []}),
        meeting("x", "2026-10-15", title="Lunch", attendees=("me@example.org", "kim@example.org")),
    ], no_record="no transcript, agenda assumed covered", trailer="→ [notes]({url})")
    assert [(o["title"], o["item_id"]) for o in ops] == [
        ("## 2026-10-08", "m1"), ("## 2026-10-15", "m2"), ("## 2026-10-22 *(no transcript, agenda assumed covered)*", "m3")]
    assert ops[1]["depends_on"] == [ops[0]["op_id"]] and ops[2]["depends_on"] == [ops[1]["op_id"]]
    apply_all(d, ops)
    assert (tmp_path / "meet" / "sam.md").read_text() == """# 1-1 · Sam

## Next

## 2026-10-22 *(no transcript, agenda assumed covered)*

- Budget

## 2026-10-15

- Launch moves to November

→ [notes](https://docs.example.org/n2)

## 2026-10-08

- Hiring: two offers out
- Reorg timing
- Action items:
\t- Send the offer letters

→ [notes](https://docs.example.org/n1)

## 2026-10-01

- Earlier topic
"""
    again = roll_operations(d, d.snapshot(), FORUMS, {"me@example.org"}, [meeting("m3", "2026-10-22")],
                            no_record="no transcript", trailer="")
    assert again == []


def test_an_unrecorded_meeting_that_is_not_the_latest_is_passed_over(tmp_path):
    d = dest(tmp_path)
    ops = roll_operations(d, d.snapshot(), FORUMS, {"me@example.org"}, [
        meeting("m1", "2026-10-08"),
        meeting("m2", "2026-10-15", recap={"covered": [{"text": "Budget approved", "next": "N3"}], "actions": []}),
    ], no_record="no transcript", trailer="")
    assert [o["title"] for o in ops] == ["## 2026-10-15"] and ops[0]["remove"] == ["- Budget"]


def test_an_unrecorded_meeting_with_nothing_on_the_agenda_writes_nothing(tmp_path):
    d = dest(tmp_path, NOTE.replace("- Hiring plan\n  - two open roles\n- Launch date\n- Budget\n", ""))
    assert roll_operations(d, d.snapshot(), FORUMS, {"me@example.org"}, [meeting("m1", "2026-10-08")],
                           no_record="no transcript", trailer="") == []


def test_what_the_operator_agreed_to_do_in_a_meeting_is_captured_once(tmp_path):
    """A recap names who owes each action. The operator's own are captured as records for a
    source to read, so a promise made in a meeting reaches triage like any other item."""
    from docket.plan.agenda_roll import capture_commitments
    meetings = [
        {"id": "m1", "date": "2026-10-08", "title": "Operator / Sam", "url": "https://docs.example.org/n1",
         "recap": {"covered": [], "actions": [{"text": "Send the offer letters", "owner": "me"},
                                              {"text": "Sam to draft the plan", "owner": "Sam"},
                                              {"text": "Review the budget", "owner": "me"}]}},
        {"id": "m2", "date": "2026-10-09", "title": "Lunch", "url": None, "recap": None},
    ]
    path = tmp_path / "state" / "commitments.jsonl"
    assert capture_commitments(path, meetings) == 2
    import json
    rows = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert rows == [
        {"id": "m1:1", "text": "Send the offer letters", "meeting": "Operator / Sam", "date": "2026-10-08",
         "url": "https://docs.example.org/n1"},
        {"id": "m1:3", "text": "Review the budget", "meeting": "Operator / Sam", "date": "2026-10-08",
         "url": "https://docs.example.org/n1"}]
    assert capture_commitments(path, meetings) == 0 and len(path.read_text().splitlines()) == 2     # once
