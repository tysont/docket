"""``docket cursor list`` and ``docket cursor reset``: where each source stands, and making the
next run read a source again from a point, with what changes shown before it changes."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pytest

from docket.cli.errors import InputError
from docket.sources.cursors import parse_point
from docket.store.paths import state_root

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
JOBS = """sources:
  jobs:
    description: Batch jobs, each read once by id.
    transport: pager-bridge
    cursor: { model: id-scan }
    request: { ids: { op: ids }, get: { op: get, args: { id: "{id}" } } }
    discover: { entries: "$.ids", id: "$" }
    paging: { entries: "$.items" }
"""


def db_of(ws) -> sqlite3.Connection:
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    db.row_factory = sqlite3.Row
    return db


def seed(ws, cli, cursors=(), units=(), processed=(), runs=()) -> None:
    cli("run", "list", "--workspace", str(ws))  # creates the database
    db = db_of(ws)
    db.executemany("INSERT INTO cursors (source, unit, position, coverage_end, updated_run) VALUES (?,?,?,?,?)", cursors)
    db.executemany("INSERT INTO units (source, unit_id, name) VALUES (?,?,?)", units)
    db.executemany("INSERT INTO processed_ids (source, record_id, run_id) VALUES (?,?,?)", processed)
    db.executemany("INSERT INTO runs (run_id, graph, date, status, sandbox, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", runs)
    db.commit()
    db.close()


def cursors(ws) -> dict:
    db = db_of(ws)
    try:
        return {(r["source"], r["unit"]): (r["position"], r["coverage_end"], r["updated_run"])
                for r in db.execute("SELECT * FROM cursors")}
    finally:
        db.close()


def with_jobs(ws):
    text = (ws / "docket.yaml").read_text()
    (ws / "docket.yaml").write_text(text.replace("sources:\n", JOBS, 1))
    return ws


# --- the point a reset goes back to ---------------------------------------------------


def test_a_point_is_an_instant_a_local_day_a_local_time_or_a_position():
    assert parse_point("2026-10-15T09:00:00Z", tz="Europe/Lisbon", now=NOW) == ("instant", "2026-10-15T09:00:00+00:00")
    assert parse_point("2026-10-15", tz="Europe/Lisbon", now=NOW) == ("instant", "2026-10-15T00:00:00+01:00")
    assert parse_point("2026-10-15T09:30", tz="Europe/Lisbon", now=NOW) == ("instant", "2026-10-15T09:30:00+01:00")
    assert parse_point("today", tz="Europe/Lisbon", now=NOW) == ("instant", "2026-10-16T00:00:00+01:00")
    assert parse_point("1840", tz="Europe/Lisbon", now=NOW) == ("position", "1840")
    assert parse_point("CAESBgg", tz="UTC", now=NOW, token=True) == ("position", "CAESBgg")


def test_a_point_that_is_none_of_those_or_in_the_future_is_refused():
    with pytest.raises(InputError) as e:
        parse_point("last tuesday", tz="UTC", now=NOW)
    assert e.value.code == "input.bad_date" and "cursor reset" in e.value.hint
    with pytest.raises(InputError) as e:
        parse_point("2026-10-20", tz="UTC", now=NOW)
    assert e.value.code == "input.bad_argument" and "future" in e.value.message


# --- cursor list -----------------------------------------------------------------------


def test_list_shows_where_each_source_stands_and_summarises_many_units(persona, cli):
    ws = persona("reference-like")
    units = [(f"spaces/room-{i}", f"Room {i}") for i in range(1, 6)]
    seed(ws, cli, cursors=[("chat", u, None, f"2026-10-1{i}T08:00:00+00:00", "2026-10-15-daily")
                           for i, (u, _) in enumerate(units, 1)] +
         [("mail", "*", "2026-10-15T07:00:00Z", "2026-10-15T08:00:00+00:00", "2026-10-15-daily")],
         units=[("chat", u, n) for u, n in units])
    code, env = cli("cursor", "list", "--workspace", str(ws))
    assert code == 0, env
    by = {s["source"]: s for s in env["data"]["sources"]}
    assert set(by) == {s["id"] for s in cli("source", "list", "--workspace", str(ws))[1]["data"]["sources"]}
    chat, mail = by["chat"], by["mail"]
    assert chat["units"] == 5 and chat["cursors"] == [] and chat["omitted"] == 5
    assert chat["coverage"] == {"earliest": "2026-10-11T08:00:00+00:00", "latest": "2026-10-15T08:00:00+00:00"}
    assert mail["cursors"][0] == {"unit": "*", "name": None, "position": "2026-10-15T07:00:00Z",
                                  "coverage_end": "2026-10-15T08:00:00+00:00", "updated_run": "2026-10-15-daily"}
    assert by["status"]["units"] == 0 and by["status"]["cursors"] == [] and by["status"]["lookback"] == "1d"
    text = cli("cursor", "list", "--workspace", str(ws), json_out=False)[1]
    assert "5 units" in text and "2026-10-11 .. 2026-10-15" in text and "no cursor" in text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len({ln.index("2026-10-15-daily") for ln in lines if "2026-10-15-daily" in ln}) == 1   # aligned

    code, env = cli("cursor", "list", "--source", "chat", "--units", "--limit", "2", "--workspace", str(ws))
    (chat,) = env["data"]["sources"]
    assert [c["unit"] for c in chat["cursors"]] == ["spaces/room-1", "spaces/room-2"] and chat["omitted"] == 3
    assert chat["cursors"][0]["name"] == "Room 1"
    text = cli("cursor", "list", "--source", "chat", "--units", "--limit", "2", "--workspace", str(ws),
               json_out=False)[1]
    assert "Room 1" in text and "3 more" in text
    code, env = cli("cursor", "list", "--source", "chat", "--unit", "spaces/room-4", "--workspace", str(ws))
    assert [c["unit"] for c in env["data"]["sources"][0]["cursors"]] == ["spaces/room-4"]
    code, env = cli("cursor", "list", "--source", "chatt", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_source"
    assert env["error"]["details"]["suggestion"] == "chat"


def test_list_counts_the_ids_an_id_scan_source_has_processed(persona, cli):
    ws = with_jobs(persona("support-lead"))
    seed(ws, cli, processed=[("jobs", "job-1", "2026-10-14-shift"), ("jobs", "job-2", "2026-10-15-shift")])
    code, env = cli("cursor", "list", "--source", "jobs", "--workspace", str(ws))
    assert code == 0, env
    (jobs,) = env["data"]["sources"]
    assert jobs["model"] == "id-scan" and jobs["processed"] == 2 and jobs["updated_run"] == "2026-10-15-shift"
    assert "2 id(s) processed" in cli("cursor", "list", "--source", "jobs", "--workspace", str(ws), json_out=False)[1]


# --- cursor reset ----------------------------------------------------------------------


def test_reset_without_a_point_removes_the_cursor_so_the_lookback_applies(persona, cli):
    ws = persona("reference-like")
    seed(ws, cli, cursors=[("chat", "spaces/a", "2026-10-15T07:00:00Z", "2026-10-15T08:00:00+00:00", "r1"),
                           ("chat", "spaces/b", None, "2026-10-15T08:00:00+00:00", "r1"),
                           ("mail", "*", "2026-10-15T07:00:00Z", "2026-10-15T08:00:00+00:00", "r1")],
         units=[("chat", "spaces/a", "Room A"), ("chat", "spaces/b", "Room B")])
    code, env = cli("cursor", "reset", "chat", "--dry-run", "--workspace", str(ws))
    assert code == 0, env
    d = env["data"]
    assert d["dry_run"] is True and d["changed"] == 2 and d["unchanged"] == 0
    assert d["changes"][0] == {"unit": "spaces/a", "name": "Room A",
                               "before": {"position": "2026-10-15T07:00:00Z", "coverage_end": "2026-10-15T08:00:00+00:00",
                                          "updated_run": "r1"}, "after": None}
    assert d["reads_from"] == "lookback 3d"
    assert len(cursors(ws)) == 3                                            # a dry run changes nothing
    text = cli("cursor", "reset", "chat", "--dry-run", "--workspace", str(ws), json_out=False)[1]
    assert text.splitlines()[0] == "would reset chat: 2 cursor(s) removed"
    assert "spaces/a" in text and "-> no cursor" in text and "lookback 3d" in text
    code, env = cli("cursor", "reset", "chat", "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] == 2
    assert set(cursors(ws)) == {("mail", "*")}
    code, env = cli("cursor", "reset", "chat", "--workspace", str(ws))     # nothing left: reports, changes nothing
    assert code == 0 and env["data"]["changed"] == 0
    db = db_of(ws)
    audit = db.execute("SELECT key FROM ledgers WHERE ledger='cursor_resets' ORDER BY key").fetchall()
    db.close()
    assert [r["key"] for r in audit] == ["chat/spaces/a", "chat/spaces/b"]


def test_reset_to_a_point_sets_every_unit_there_including_known_units_without_a_cursor(persona, cli):
    ws = persona("reference-like")
    seed(ws, cli, cursors=[("chat", "spaces/a", "2026-10-15T07:00:00Z", "2026-10-15T08:00:00+00:00", "r1")],
         units=[("chat", "spaces/a", "Room A"), ("chat", "spaces/b", "Room B")])
    code, env = cli("cursor", "reset", "chat", "--to", "2026-10-10", "--workspace", str(ws))
    assert code == 0, env
    point = "2026-10-10T00:00:00-05:00"                                     # the persona's timezone
    assert env["data"]["to"] == point and env["data"]["changed"] == 2
    got = cursors(ws)
    assert got[("chat", "spaces/a")][:2] == (None, point) and got[("chat", "spaces/b")][:2] == (None, point)
    assert got[("chat", "spaces/a")][2].startswith("reset:")
    code, env = cli("cursor", "reset", "chat", "--unit", "spaces/a", "--to", "2026-10-10", "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] == 0 and env["data"]["unchanged"] == 1
    code, env = cli("cursor", "reset", "mail", "--to", "2026-10-12T06:00:00Z", "--workspace", str(ws))
    assert code == 0 and cursors(ws)[("mail", "*")][:2] == (None, "2026-10-12T06:00:00+00:00")
    code, env = cli("cursor", "reset", "mail", "--to", "1840", "--workspace", str(ws))
    assert code == 0 and cursors(ws)[("mail", "*")][:2] == ("1840", None)
    text = cli("cursor", "reset", "chat", "--unit", "spaces/b", "--to", "2026-10-09", "--dry-run",
               "--workspace", str(ws), json_out=False)[1]
    assert "spaces/b" in text and "Room B" in text and "coverage 2026-10-10T00:00:00-05:00" in text
    assert "-> coverage 2026-10-09T00:00:00-05:00" in text


def test_reset_refuses_an_unknown_source_or_unit_with_the_closest_name(persona, cli):
    ws = persona("reference-like")
    seed(ws, cli, cursors=[("chat", "spaces/room-1", None, "2026-10-15T08:00:00+00:00", "r1")])
    code, env = cli("cursor", "reset", "chta", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_source"
    assert env["error"]["details"]["suggestion"] == "chat"
    code, env = cli("cursor", "reset", "chat", "--unit", "spaces/room-2", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_unit"
    assert env["error"]["details"]["suggestion"] == "spaces/room-1"
    assert "docket cursor list" in env["error"]["hint"]
    code, env = cli("cursor", "reset", "chat", "--to", "soon", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_date"


def test_reset_refuses_while_an_open_run_has_read_the_source(persona, cli, pinned_now):
    ws = persona("support-lead")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    code, env = cli("do", "read-tickets", "--workspace", str(ws))
    assert code == 0, env
    code, env = cli("cursor", "reset", "tickets", "--dry-run", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "cursor.read_in_open_run"
    assert run_id in env["error"]["message"] and f"docket run abandon {run_id}" in env["error"]["hint"]
    assert env["error"]["details"]["runs"] == [run_id]
    code, env = cli("cursor", "reset", "pager", "--workspace", str(ws))    # not read yet: the run will read from it
    assert code == 0, env
    cli("run", "abandon", run_id, "--workspace", str(ws))
    code, env = cli("cursor", "reset", "tickets", "--workspace", str(ws))
    assert code == 0, env


def test_a_sandbox_run_that_read_the_source_does_not_block_a_reset(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--sandbox", "--now", pinned_now, "--workspace", str(ws))
    assert cli("do", "read-tickets", "--workspace", str(ws))[0] == 0
    code, env = cli("cursor", "reset", "tickets", "--workspace", str(ws))
    assert code == 0, env


def test_reset_of_an_id_scan_source_forgets_its_processed_ids(persona, cli):
    """An id-scan source reads only ids it has not processed, so a reset forgets the markers:
    all of them, one id, or those a run on or after the point recorded."""
    ws = with_jobs(persona("support-lead"))
    seed(ws, cli, processed=[("jobs", "job-1", "2026-10-13-shift"), ("jobs", "job-2", "2026-10-14-shift"),
                             ("jobs", "job-3", "2026-10-15-shift")],
         runs=[(f"2026-10-1{d}-shift", "shift", f"2026-10-1{d}", "sealed", 0, "x", "x") for d in (3, 4, 5)])
    code, env = cli("cursor", "reset", "jobs", "--to", "2026-10-14", "--dry-run", "--workspace", str(ws))
    assert code == 0, env
    assert [f["id"] for f in env["data"]["forgets"]] == ["job-2", "job-3"] and env["data"]["changed"] == 2
    text = cli("cursor", "reset", "jobs", "--to", "2026-10-14", "--dry-run", "--workspace", str(ws), json_out=False)[1]
    assert text.splitlines()[0] == "would reset jobs: 2 processed id(s) forgotten, 1 kept"
    code, env = cli("cursor", "reset", "jobs", "--unit", "job-1", "--workspace", str(ws))
    assert code == 0 and [f["id"] for f in env["data"]["forgets"]] == ["job-1"]
    code, env = cli("cursor", "reset", "jobs", "--unit", "job-9", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_unit"
    code, env = cli("cursor", "reset", "jobs", "--to", "12", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument" and "id-scan" in env["error"]["message"]
    code, env = cli("cursor", "reset", "jobs", "--workspace", str(ws))
    assert code == 0 and [f["id"] for f in env["data"]["forgets"]] == ["job-2", "job-3"]
    db = db_of(ws)
    assert db.execute("SELECT COUNT(*) FROM processed_ids").fetchone()[0] == 0
    db.close()
