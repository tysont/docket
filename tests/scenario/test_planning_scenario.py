"""Planning scenarios (IP M7 exit, TP §6.1): the reference-like persona plans its week from a JSON calendar."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from docket.config import load_workspace
from docket.plan.calendar_classify import classify_calendar
from docket.plan.providers import JsonFileProvider
from docket.plan.types import at
from docket.shapes.unexpected import collecting
from docket.store.paths import state_root
from tests.agent_loop import run_agent

TZ = ZoneInfo("America/Chicago")


def _start(cli, ws, pinned_now):
    code, env = cli("run", "start", "week-plan", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["run"]


def _plan(ws, run_id, pd):
    return json.loads((state_root(ws, os.environ) / "runs" / run_id / "plan" / pd / "plan.json").read_text())


@pytest.mark.parametrize("name,tz", [("reference-like", "America/Chicago"), ("support-lead", "Europe/Lisbon")])
def test_persona_calendars_classify_with_only_the_injected_unknown(persona, name, tz):
    ws = persona(name)
    cfg = load_workspace(ws).config
    z = ZoneInfo(tz)
    lo = date(2026, 10, 15)
    hi = lo + timedelta(days=42)
    events = JsonFileProvider(ws / "data" / "calendar.json", z).list_events(at(lo, 0, z), at(hi, 0, z))
    with collecting() as col:
        cal = classify_calendar(events, cfg.profile.calendar.classify, lo=lo, hi=hi, tz=z)
    details = [s.detail for s in col.drain()]
    assert len(details) == 1 and details[0].startswith("unclassified event: ")
    c = cal.counts
    assert c["considered"] == c["masters"] + c["exceptions"] + c["singles"] + c["ignored"]
    assert c["tombstones"] >= 2


def test_week_plan_schedules_over_the_flight_keeps_releases_and_mirrors(persona, cli, pinned_now):
    ws = persona("reference-like")
    run_id = _start(cli, ws, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "task", "--when", "Flight", "--est", "90",
                    "--title", "Draft the offsite memo", "--workspace", str(ws))
    assert code == 0, env
    assert env["data"]["answer"]["overrides"]["when"] == "event:Flight"
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == [], res.failures

    code, env = cli("plan", "diff", "--workspace", str(ws))
    assert code == 0, env
    pd = env["data"]["plan_digest"]
    plan = _plan(ws, run_id, pd)
    assert set(plan["inputs"]) >= {"task_store_digest", "calendar_digest", "answers_digest", "config_digest"}
    cal_ops = [o for o in plan["operations"] if "key" in o]
    by_title = {o["title"]: o for o in cal_ops}

    memo = by_title["Draft the offsite memo"]
    assert memo["verb"] == "create" and memo["start"] == "2026-10-21T13:00:00-05:00"
    assert memo["end"] == "2026-10-21T14:30:00-05:00" and memo["destination"] == "work-calendar"
    seed = by_title["Review the ingest service runbook"]
    assert seed["verb"] == "keep" and seed["key"] == "dk_t_61a4bf1529ea"
    closed = [o for o in cal_ops if o["key"] == "dk_t_000000c105ed"]
    assert [o["verb"] for o in closed] == ["release"]
    mirror = [o for o in cal_ops if o.get("kind") == "mirror"]
    assert [(o["verb"], o["title"], o["start"]) for o in mirror] == [("create", "Busy", "2026-10-16T15:30:00-05:00")]
    # task-store operations sit alongside, unchanged in shape
    assert sum(1 for o in plan["operations"] if "line" in o) == 3
    # every calendar block passes the policy: inside 09:00-17:00, nothing at lunch, nothing on the PTO day
    for row in plan["schedule"]:
        assert not row["date"].startswith("2026-10-23")
        if "Draft the offsite memo" != row["title"]:
            assert "T12:" not in row["start"]
    text = env["data"]["groups"]
    assert any(g["label"].startswith("Wed 2026-10-21") and any("new block: Draft the offsite memo" in x
                                                                for x in g["lines"]) for g in text)

    code, env = cli("plan", "explain", "offsite memo", "--workspace", str(ws))
    assert code == 0, env
    ex = env["data"]["explain"]
    assert ex["placed"]["date"] == "2026-10-21" and ex["ordering_key"]["window"] == "event:Flight"
    code, env = cli("plan", "explain", "no such thing", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "plan.unknown_item"

    code, env = cli("plan", "load", "wed", "--workspace", str(ws))
    assert code == 0, env
    load = env["data"]["load"]
    assert load["date"] == "2026-10-21" and load["committed_minutes"] == 180 and load["free_minutes"] == 240

    unexpected = [json.loads(ln) for ln in (state_root(ws, os.environ) / "runs" / run_id / "unexpected.jsonl")
                  .read_text().splitlines()]
    assert [u["detail"] for u in unexpected if u["area"] == "plan.calendar"] == ["unclassified event: Aa AA 9"]

    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"


def test_plan_build_is_current_after_build_and_stale_after_a_calendar_change(persona, cli, pinned_now):
    ws = persona("reference-like")
    _start(cli, ws, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == []
    code, env = cli("plan", "build", "--workspace", str(ws))
    assert code == 0 and env["data"]["status"] == "current"   # the mirror ledger write does not stale it
    p = ws / "data" / "calendar.json"
    data = json.loads(p.read_text())
    data["events"].append({"id": "new", "title": "Late add", "start": "2026-10-16T13:00:00",
                           "end": "2026-10-16T14:00:00", "attendees": 3})
    p.write_text(json.dumps(data))
    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["step"]["kind"] == "run" and env["data"]["step"]["stage"] == "plan"


def test_window_too_small_fails_plan_build_with_free_minutes(persona, cli, pinned_now):
    ws = persona("reference-like")
    _start(cli, ws, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "task", "--when", "fri 14:00-14:45", "--est", "90", "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "plan")
    code, env = cli("plan", "build", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "plan.window_too_small"
    w = env["error"]["details"]["windows"][0]
    assert w["free_minutes"] == 10 and w["est"] == 90 and w["question_id"] == q["id"]   # a 1:1 and its 5m buffer
    assert q["id"] in env["error"]["hint"]


def test_unknown_named_event_window_is_refused(persona, cli, pinned_now):
    ws = persona("reference-like")
    _start(cli, ws, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    cli("ask", "answer", q["id"], "task", "--when", "Moon landing", "--workspace", str(ws))
    run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "plan")
    code, env = cli("plan", "build", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "plan.window_unresolved"


def test_ask_answer_still_refuses_a_malformed_day_window(persona, cli, pinned_now):
    ws = persona("reference-like")
    _start(cli, ws, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    q = res.final["questions"][0]
    code, env = cli("ask", "answer", q["id"], "task", "--when", "thu 17:00-13:00", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_window"


def test_planning_inactive_workflows_plan_as_before(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "triage", "--now", pinned_now, "--sandbox", "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    assert res.failures == []
    q = res.final["questions"][0]
    assert q["details"]["operations"] == 8
    code, env = cli("plan", "explain", "anything", "--workspace", str(ws))
    assert code == 3 and env["error"]["code"] == "plan.no_calendar"
    Path(ws)  # the triage workflow is not in planning.workflows
