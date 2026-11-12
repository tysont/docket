"""A meeting with no record (TD §10.4, §12.8): instead of assuming its agenda was covered, an ask
node asks which entries were, and the plan rolls the agenda through approve and apply. All
names and data are invented."""
from __future__ import annotations

import json
import os

from docket.store.paths import state_root
from tests.agent_loop import run_agent

AGENDA = """# Platform Sync

## Next

- Capacity plan
  - two scenarios
- Hiring
- Offsite

## 2026-10-01

- Earlier topic
"""

GRAPH = """
  agendas:
    # Ask which agenda entries a meeting with no record covered.
    read-meetings: { read: meetings }
    covered: { ask: basic, questions: { kinds: [covered], covered: { meetings: read-meetings, to: agendas, suffix: "no record" } } }
    plan: { plan: [covered] }
    approve: { approve: plan }
    apply: { apply: approve }
"""

PEOPLE = ["sam@example.org", "kim@example.org"]
MEETINGS = [
    {"id": "m2", "title": "Platform Sync", "at": "2026-10-14T15:00:00Z", "who": PEOPLE, "text": ""},
    {"id": "m3", "title": "Lunch and learn", "at": "2026-10-13T18:00:00Z", "who": ["x@example.org"], "text": ""},
]


def write_meetings(ws, rows):
    (ws / "data" / "meetings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def setup(ws, rows=MEETINGS):
    (ws / "Syncs").mkdir(exist_ok=True)
    (ws / "Syncs" / "Platform.md").write_text(AGENDA)
    write_meetings(ws, rows)
    src = ws / "sources.yaml"
    src.write_text(src.read_text() + """  meetings:
    kind: files
    description: Meetings held, each with its record when there is one; every run reads them all.
    path: data/meetings.jsonl
    cursor: { model: snapshot }
    mapping: { id: "$.id", title: "$.title", text: "$.text", at: "$.at", fields.attendees: "$.who" }
""")
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    cfg.write_text(text.replace("\ngraphs:\n", "\ngraphs:" + GRAPH, 1))
    return ws / "Syncs" / "Platform.md"


def start(cli, ws, now, *extra):
    code, env = cli("run", "start", "agendas", *extra, "--now", now, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["run"]


def by_entry(answers):
    """A driver that answers each covered question by the entry its prompt shows."""
    def choose(q):
        if q["kind"] != "covered":
            return q["options"][0]
        want = next(v for k, v in answers.items() if f'"{k}"' in q["prompt"])
        return next(o for o in q["options"] if o["id"] == want)
    return choose


def covered_questions(res):
    return [q for s in res.steps if s["kind"] == "ask" for q in s["questions"] if q["kind"] == "covered"]


def test_the_answers_of_one_meeting_become_one_roll_through_approve_and_apply(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    run_id = start(cli, ws, pinned_now)
    first = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    qs = first.final["questions"]
    assert [q["prompt"] for q in qs] == [
        'Did you cover this with Platform Sync on 2026-10-14? "Capacity plan"',
        'Did you cover this with Platform Sync on 2026-10-14? "Hiring"',
        'Did you cover this with Platform Sync on 2026-10-14? "Offsite"']
    q = qs[0]
    assert [o["id"] for o in q["options"]] == ["covered", "keep", "drop"]
    assert q["details"]["recommended"] == "covered" and q["details"]["batch"] == ["covered"]
    assert q["details"]["meeting"] == {"id": q["details"]["meeting"]["id"], "date": "2026-10-14",
                                       "title": "Platform Sync", "forum": "platform-sync",
                                       "forum_name": "Platform Sync", "path": "Syncs/Platform.md"}
    assert q["details"]["evidence"] == ["- Capacity plan", "  - two scenarios"]

    res = run_agent(ws, choose=by_entry({"Capacity plan": "covered", "Hiring": "keep", "Offsite": "drop"}))
    assert res.failures == [] and res.final["kind"] == "done", res.final
    summary = res.final["summary"]
    assert summary["operations"] == summary["applied"] == 1
    assert note.read_text() == """# Platform Sync

## Next

- Hiring

## 2026-10-14 *(no record)*

- Capacity plan
  - two scenarios

## 2026-10-01

- Earlier topic
"""
    rdir = state_root(ws, os.environ) / "runs" / run_id
    approve = [s for s in res.steps if s["node"] == "approve"]
    assert approve, "the roll went through the plan's approval"
    plans = list((rdir / "plan").glob("*/plan.json"))
    ops = [o for p in plans for o in json.loads(p.read_text())["operations"]]
    assert {o["verb"] for o in ops} == {"roll"}

    start(cli, ws, "2026-10-16T18:00:00-05:00", "--new")
    again = run_agent(ws)
    assert again.failures == [] and again.final["kind"] == "done", again.final
    assert covered_questions(again) == []                                   # the meeting has its section now


def test_all_kept_writes_nothing_and_is_not_asked_again_until_the_next_meeting(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    start(cli, ws, pinned_now)
    res = run_agent(ws, choose=lambda q: next(o for o in q["options"] if o["id"] in ("keep", "yes")), full=True)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    asked = covered_questions(res)
    assert len(asked) == 3 and res.final["summary"].get("operations", 0) == 0
    assert note.read_text() == AGENDA

    start(cli, ws, "2026-10-16T18:00:00-05:00", "--new")
    again = run_agent(ws, full=True)
    assert again.failures == [] and again.final["kind"] == "done", again.final
    assert covered_questions(again) == []                                   # remembered for that meeting
    assert note.read_text() == AGENDA

    write_meetings(ws, MEETINGS + [{"id": "m4", "title": "Platform Sync", "at": "2026-10-21T15:00:00Z", "who": PEOPLE,
                                    "text": ""}])
    start(cli, ws, "2026-10-21T18:00:00-05:00", "--new")
    later = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    prompts = [q["prompt"] for q in later.final["questions"]]
    assert prompts == [f'Did you cover this with Platform Sync on 2026-10-21? "{t}"'
                       for t in ("Capacity plan", "Hiring", "Offsite")]
    assert not {q["id"] for q in later.final["questions"]} & {q["id"] for q in asked}


def test_a_meeting_with_a_record_is_not_asked_about(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws, [{**MEETINGS[0], "text": "Capacity: the plan was agreed."}])
    start(cli, ws, pinned_now)
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    assert covered_questions(res) == [] and res.final["summary"].get("operations", 0) == 0
    assert note.read_text() == AGENDA


def test_a_sandbox_run_leaves_the_note_byte_identical(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    before = note.read_bytes()
    run_id = start(cli, ws, pinned_now, "--sandbox")
    res = run_agent(ws, choose=by_entry({"Capacity plan": "covered", "Hiring": "drop", "Offsite": "covered"}))
    assert res.failures == [] and res.final["kind"] == "done", res.final
    assert res.final["summary"]["sandbox"] is True and res.final["summary"]["applied"] == 1
    assert note.read_bytes() == before
    twin = state_root(ws, os.environ) / "runs" / run_id / "sandbox" / "agendas" / "Syncs" / "Platform.md"
    assert twin.read_text() == """# Platform Sync

## Next

## 2026-10-14 *(no record)*

- Capacity plan
  - two scenarios
- Offsite

## 2026-10-01

- Earlier topic
"""
    # a sandbox run remembers nothing: a live run asks the same questions
    start(cli, ws, pinned_now)
    live = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    assert len(live.final["questions"]) == 3
