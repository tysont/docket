"""Membership (TD §6.9): the unit registry follows the system. A unit a complete discovery
no longer lists is marked gone and stops counting; one that comes back is current again;
a unit first seen after the baseline is reported as new. The operator can classify a unit
by hand, and no rule overrides that.
"""
from __future__ import annotations

import sqlite3

import pytest

from docket.config import check
from docket.sources.base import Unit
from docket.sources.units import preview, set_classification, unset_classification, upsert_units
from docket.store.db import open_db

pytestmark = pytest.mark.smoke

CONFIG = """\
version: 1
graphs:
  daily: { touch: { run: ["true"] } }
profile:
  timezone: UTC
  me: { name: Dana Reyes }
signals:
  units:
    rules:
      - { id: war, match: { name_regex: "war room" }, set: { class: incident, tier: 1 } }
"""
T1, T2, T3 = "2026-10-15T08:00:00+00:00", "2026-10-16T08:00:00+00:00", "2026-10-17T08:00:00+00:00"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docket.yaml").write_text(CONFIG, encoding="utf-8")
    ws, issues, _ = check(root)
    assert issues == []
    db = open_db(tmp_path / "d.db")
    db.row_factory = sqlite3.Row
    yield ws, db
    db.close()


def units(*names):
    return [Unit(n, n.replace("-", " "), "ROOM", 5) for n in names]


def rows(db, source="chat"):
    return {r["unit_id"]: dict(r) for r in db.execute("SELECT * FROM units WHERE source=?", (source,))}


def test_the_first_discovery_is_a_baseline_and_later_ones_report_what_changed(env):
    ws, db = env
    first = upsert_units(db, ws, "chat", units("ops", "ingest-war-room", "lunch"), T1)
    assert first["baseline"] is True and first["new"] == [] and first["gone"] == [] and first["current"] == 3

    second = upsert_units(db, ws, "chat", units("ops", "ingest-war-room", "billing-war-room"), T2)
    assert second["baseline"] is False and second["current"] == 3
    assert second["new"] == [{"unit": "billing-war-room", "name": "billing war room", "class": "incident", "tier": "1"}]
    assert second["gone"] == [{"unit": "lunch", "name": "lunch", "class": None, "tier": None}]
    got = rows(db)
    assert got["lunch"]["gone_at"] == T2 and got["lunch"]["last_seen"] == T1
    assert got["ops"]["gone_at"] is None and got["billing-war-room"]["first_seen"] == T2

    third = upsert_units(db, ws, "chat", units("ops", "ingest-war-room", "billing-war-room", "lunch"), T3)
    assert third["new"] == [] and third["gone"] == []
    assert third["returned"] == [{"unit": "lunch", "name": "lunch", "class": None, "tier": None}]
    assert rows(db)["lunch"]["gone_at"] is None and rows(db)["lunch"]["first_seen"] == T1


def test_another_source_is_never_marked_gone(env):
    ws, db = env
    upsert_units(db, ws, "chat", units("ops"), T1)
    upsert_units(db, ws, "dms", units("dm-kim"), T1)
    out = upsert_units(db, ws, "chat", units("ops"), T2)
    assert out["gone"] == [] and rows(db, "dms")["dm-kim"]["gone_at"] is None


def test_gone_units_are_left_out_of_the_preview_unless_asked_for(env):
    ws, db = env
    upsert_units(db, ws, "chat", units("ops", "lunch"), T1)
    upsert_units(db, ws, "chat", units("ops"), T2)
    assert [r["unit"] for r in preview(db, ws)] == ["ops"]
    assert [r["unit"] for r in preview(db, ws, gone=True)] == ["lunch", "ops"]


def test_the_operator_classifies_a_unit_and_no_rule_overrides_it(env):
    ws, db = env
    upsert_units(db, ws, "chat", units("ingest-war-room", "lunch"), T1)
    assert set_classification(db, "chat", "lunch", {"class": "social", "tier": 4}) is True
    assert set_classification(db, "chat", "ingest-war-room", {"tier": 3}) is True   # only the tier; the class stays
    assert set_classification(db, "chat", "nope", {"class": "x"}) is False

    upsert_units(db, ws, "chat", units("ingest-war-room", "lunch"), T2)              # the next discovery
    got = rows(db)
    assert (got["lunch"]["class"], got["lunch"]["tier"], got["lunch"]["classified_by"]) == ("social", "4", "operator")
    assert (got["ingest-war-room"]["class"], got["ingest-war-room"]["tier"]) == ("incident", "3")
    assert {r["unit"]: r["now"]["class"] for r in preview(db, ws)} == {"ingest-war-room": "incident", "lunch": "social"}

    assert unset_classification(db, ws, "chat", "ingest-war-room") is True           # back to the rules
    got = rows(db)["ingest-war-room"]
    assert (got["class"], got["tier"], got["classified_by"]) == ("incident", "1", "rule")
