"""Mirrors (TD §12.5, FR-PLN-07): create, move, keep, release, and a deletion honored."""
from __future__ import annotations

import pytest

from docket.config.models import PlanningMirror
from docket.plan.mirrors import plan_mirrors
from docket.plan.types import Event, mirror_key
from tests.plan_helpers import TODAY, TZ, calendar, d, dt, ev

pytestmark = pytest.mark.smoke

RULE = PlanningMirror.model_validate({"from": "personal", "to": "work", "as": {"title": "Busy"}})


def personal(id, day, s, e, **kw):
    return ev(id, day, s, e, "Dentist", calendar="personal", attendees=0, **kw)


def hold(src_id, day, s, e):
    k = mirror_key("dk_", "work", src_id)
    return Event(id="h_" + src_id, calendar="work", title="Busy", start=dt(day, s), end=dt(day, e), owned_key=k)


def plan(events, ledger=None, rule=RULE):
    cal = calendar(events)
    return plan_mirrors([rule], cal.instances, prefix="dk_", tz=TZ, ledger=ledger or {})


def counts_reconcile(mp):
    c = mp.counts
    return c["qualifying"] == c["created"] + c["moved"] + c["kept"] + c["suppressed"]


def test_create_keep_move_release():
    events = [personal("p1", d(1), "10:00", "11:00"), personal("p2", d(2), "10:00", "11:00"),
              personal("p3", d(3), "15:00", "16:00"),
              hold("p2", d(2), "10:00", "11:00"), hold("p3", d(3), "09:00", "10:00"), hold("gone", d(4), "09:00", "10:00")]
    mp = plan(events)
    got = sorted((o["verb"], o.get("source")) for o in mp.ops)
    assert got == [("create", "p1"), ("keep", "p2"), ("move", "p3"), ("release", None)]
    assert all(o["title"] == "Busy" and o["calendar"] == "work" for o in mp.ops if o["verb"] != "release")
    assert counts_reconcile(mp) and mp.counts["released"] == 1
    assert sorted(mp.owned_keys) == sorted(mirror_key("dk_", "work", s) for s in ("p2", "p3", "gone"))
    assert {k for k, _ in mp.ledger_updates} == {mirror_key("dk_", "work", s) for s in ("p2", "p3", "gone")}


def test_a_deleted_hold_is_not_recreated():
    events = [personal("p1", d(1), "10:00", "11:00")]
    key = mirror_key("dk_", "work", "p1")
    first = plan(events + [hold("p1", d(1), "10:00", "11:00")])
    ledger = dict(first.ledger_updates)
    assert ledger == {key: "present"}
    after_delete = plan(events, ledger=ledger)        # the operator deleted the hold
    assert after_delete.ops == [] and after_delete.counts["suppressed"] == 1 and after_delete.suppressed == [key]
    assert counts_reconcile(after_delete)
    no_respect = PlanningMirror.model_validate({"from": "personal", "to": "work", "respect_deletions": False})
    assert [o["verb"] for o in plan(events, ledger=ledger, rule=no_respect).ops] == ["create"]


def test_a_released_hold_whose_source_returns_is_recreated():
    key = mirror_key("dk_", "work", "p1")
    mp = plan([personal("p1", d(1), "10:00", "11:00")], ledger={key: "released"})
    assert [o["verb"] for o in mp.ops] == ["create"]


def test_transparent_all_day_and_owned_sources_are_not_mirrored():
    events = [personal("free", d(1), "10:00", "11:00", transparency="transparent"),
              Event("ad", "personal", "Birthday", d(2), d(3), all_day=True)]
    assert plan(events).ops == []
    both = PlanningMirror.model_validate({"from": "personal", "to": "work", "include_all_day": True})
    assert [o["verb"] for o in plan(events, rule=both).ops] == ["create"]


def test_duplicate_holds_release_the_extra_one():
    h = hold("p1", d(1), "10:00", "11:00")
    dup = Event(id="h_dup", calendar="work", title="Busy", start=h.start, end=h.end, owned_key=h.owned_key)
    mp = plan([personal("p1", d(1), "10:00", "11:00"), h, dup])
    assert sorted(o["verb"] for o in mp.ops) == ["keep", "release"]
    assert TODAY < d(1)
