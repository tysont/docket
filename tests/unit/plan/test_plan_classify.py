"""Calendar classification (TD §12.1, TP §5.4): kinds, recurring series, tombstones, spans, unknowns."""
from __future__ import annotations

from datetime import timedelta

import pytest

from docket.plan.calendar_classify import Classifier, classify_calendar, expand, word_shape
from docket.plan.types import Event
from docket.shapes.unexpected import collecting
from tests.plan_helpers import RULES, TODAY, TZ, allday, calendar, d, dt, ev, hm, owned, rule

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("event,kind", [
    (ev("a", TODAY, "10:00", "11:00", "Design review"), "meeting"),
    (ev("b", TODAY, "09:00", "11:00", "Focus: writing", attendees=0), "focus"),
    (ev("c", TODAY, "13:00", "14:00", "Hold for interviews", attendees=0), "soft_hold"),
    (ev("e", TODAY, "07:00", "08:30", "Commute", attendees=0), "boundary"),
    (ev("f", TODAY, "18:00", "19:00", "Dentist", calendar="personal", attendees=0), "mirrored"),
    (ev("g", TODAY, "12:00", "13:00", "Lunch talk", attendees=40, transparency="transparent"), "informational"),
    (allday("h", TODAY, title="Away OOO"), "out_of_office"),
    (allday("i", TODAY, title="Release freeze"), "informational"),
    (owned("dk_t_abc", TODAY, "10:00", "11:00", title="Focus: anything"), "owned"),
])
def test_rules_assign_one_kind_in_order(event, kind):
    with collecting() as col:
        assert Classifier(RULES, "dk_").kind(event) == kind
    assert col.drain() == []


def test_owned_comes_first_and_needs_this_workspaces_prefix():
    clf = Classifier(RULES, "dk_")
    assert clf.kind(owned("dk_t_1", TODAY, "10:00", "11:00", title="Focus x")) == "owned"
    assert clf.kind(owned("other_t_1", TODAY, "10:00", "11:00", title="Focus x")) == "focus"


def test_property_match_semantics():
    clf = Classifier([rule("boundary", property={"room": "Annex"}), rule("focus", property={"attendees": False})], "dk_")
    assert clf.kind(ev("a", TODAY, "10:00", "11:00", properties={"room": "annex"})) == "boundary"
    assert clf.kind(ev("b", TODAY, "10:00", "11:00", attendees=0)) == "focus"


def test_unknown_event_is_busy_and_records_its_word_shape():
    e = ev("x", TODAY, "10:00", "11:00", "Vendor QBR 3", attendees=0)
    with collecting() as col:
        cal = calendar([e])
    shapes = col.drain()
    assert [s.detail for s in shapes] == ["unclassified event: Aa AA 9"]
    assert shapes[0].area == "plan.calendar" and shapes[0].extend == "profile.calendar.classify"
    assert "Vendor" not in (shapes[0].sample or "")
    assert [(s.kind, s.start, s.end) for s in cal.segments(TODAY)] == [("meeting", 600, 660)]


@pytest.mark.parametrize("title,shape", [
    ("Lunch with 9", "Aa aa 9"), ("1:1 re: Q4", "9:9 a: A9"), ("", "(empty)"), ("SYNC", "AA"),
])
def test_word_shape(title, shape):
    assert word_shape(title) == shape


def test_weekly_series_that_began_eight_weeks_ago_expands_within_the_horizon_only():
    start = TODAY - timedelta(weeks=8)
    master = Event("s1", "work", "Platform weekly", dt(start, "10:00"), dt(start, "10:30"),
                   properties={"attendees": 5}, recurrence={"freq": "weekly", "byday": ["thu", "mon"]})
    cal = calendar([master], horizon=14)
    days = sorted(day for day, segs in cal.days.items() for _ in segs)
    assert days == [d(0), d(4), d(7), d(11)]            # Thu, Mon, Thu, Mon inside 14 days
    assert cal.counts["generated"] == 4 and cal.counts["masters"] == 1


def test_series_count_and_until_are_honored_from_the_series_start():
    start = TODAY - timedelta(days=10)
    counted = Event("c", "work", "Daily standup", dt(start, "09:00"), dt(start, "09:15"), properties={"attendees": 4},
                    recurrence={"freq": "daily", "count": 12})
    until = Event("u", "work", "Daily standup", dt(TODAY, "16:00"), dt(TODAY, "16:15"), properties={"attendees": 4},
                  recurrence={"freq": "daily", "until": d(2).isoformat()})
    cal = calendar([counted, until])
    assert sorted(s.day for segs in cal.days.values() for s in segs if s.event_id.startswith("c@")) == [d(0), d(1)]
    assert sorted(s.day for segs in cal.days.values() for s in segs if s.event_id.startswith("u@")) == [d(0), d(1), d(2)]


def test_tombstones_and_exceptions_without_breaking_counts():
    start = TODAY - timedelta(weeks=8)
    master = Event("s1", "work", "Platform weekly", dt(start, "10:00"), dt(start, "10:30"),
                   properties={"attendees": 5}, recurrence={"freq": "weekly"})
    cancel = Event("s1-x", "work", "", dt(d(7), "10:00"), dt(d(7), "10:30"), status="cancelled", series_id="s1",
                   original_start=dt(d(7), "10:00"))
    moved = Event("s1-y", "work", "Platform weekly", dt(d(0), "14:00"), dt(d(0), "14:30"), series_id="s1",
                  original_start=dt(d(0), "10:00"), properties={"attendees": 5})
    lone = Event("gone", "work", "Old meeting", dt(d(1), "11:00"), dt(d(1), "12:00"), status="cancelled")
    cancelled_master = Event("s2", "work", "Retired series", dt(start, "15:00"), dt(start, "16:00"), status="cancelled",
                             recurrence={"freq": "daily"})
    events = [master, cancel, moved, lone, cancelled_master]
    exp = expand(events, TODAY, TODAY + timedelta(days=14), TZ)
    assert exp.considered == exp.masters + exp.exceptions + exp.singles + exp.ignored == 5
    assert exp.tombstones == 3
    starts = sorted((e.start.date(), e.start.hour) for e in exp.events)
    assert starts == [(d(0), 14)]                       # d(7) cancelled, d(0) moved to 14:00
    cal = classify_calendar(events, RULES, lo=TODAY, hi=TODAY + timedelta(days=14), tz=TZ)
    assert cal.counts["concrete"] == 1


def test_eight_week_span_is_clipped_per_day_inside_the_horizon():
    leave = Event("l", "work", "Parental leave OOO", dt(TODAY - timedelta(weeks=4), "00:00"),
                  dt(TODAY + timedelta(weeks=4), "00:00"))
    trip = Event("t", "work", "Offsite OOO", dt(d(1), "15:00"), dt(d(2), "11:00"))
    cal = calendar([leave, trip], horizon=10)
    assert sorted(cal.days) == [d(i) for i in range(10)]
    assert all(any(s.event_id == "l" and (s.start, s.end) == (0, 1440) for s in cal.segments(d(i))) for i in range(10))
    assert [(s.start, s.end) for s in cal.segments(d(1)) if s.event_id == "t"] == [(hm("15:00"), 1440)]
    assert [(s.start, s.end) for s in cal.segments(d(2)) if s.event_id == "t"] == [(0, hm("11:00"))]


def test_multi_day_all_day_event_clips_per_day():
    cal = calendar([allday("v", d(-2), days=5, title="Vacation OOO")])
    assert sorted(cal.days) == [d(0), d(1), d(2)]


def test_owned_events_are_not_fixed_availability():
    cal = calendar([owned("dk_t_1", TODAY, "10:00", "11:00")])
    assert cal.segments(TODAY) == [] and len(cal.owned) == 1
