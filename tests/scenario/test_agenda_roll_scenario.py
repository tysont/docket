"""Agendas are rolled after the meetings held (TD §12.8): a graph reads the meetings, the recap
judge reads the recorded ones beside the forum's agenda, and a ``write: roll`` node writes the
dated sections. All names and data are invented."""
from __future__ import annotations

import json

from tests.agent_loop import run_agent

AGENDA = """# Platform Sync

## Next

- Capacity plan
- Hiring
- Offsite

## 2026-10-01

- Earlier topic
"""

GRAPH = """
  agendas:
    # Roll agendas after the meetings held.
    read-meetings: { read: meetings }
    recap: { judge: recap, from: [read-meetings] }
    roll: { write: roll, from: [recap], to: agendas, no_record: "no record, agenda assumed covered", trailer: "→ [notes]({url})" }
"""


def setup(ws):
    (ws / "Syncs").mkdir(exist_ok=True)
    (ws / "Syncs" / "Platform.md").write_text(AGENDA)
    rows = [
        {"id": "m1", "title": "Platform Sync", "at": "2026-10-08T15:00:00Z", "who": ["sam@example.org", "kim@example.org"],
         "text": "Capacity: the plan was agreed.\nA new topic came up: the vendor review.", "doc": "https://docs.example.org/m1"},
        {"id": "m2", "title": "Platform Sync", "at": "2026-10-14T15:00:00Z", "who": ["sam@example.org", "kim@example.org"],
         "text": ""},
        {"id": "m3", "title": "Lunch and learn", "at": "2026-10-13T18:00:00Z", "who": ["x@example.org", "y@example.org"],
         "text": "A talk."},
    ]
    (ws / "data" / "meetings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    src = ws / "sources.yaml"
    src.write_text(src.read_text() + """  meetings:
    kind: files
    description: Meetings held, each with its record when there is one.
    path: data/meetings.jsonl
    mapping: { id: "$.id", title: "$.title", text: "$.text", at: "$.at", fields.attendees: "$.who", fields.record_url: "$.doc" }
""")
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    assert "\ngraphs:\n" in text and "\ntasks:\n" in text
    text = text.replace("\ngraphs:\n", "\ngraphs:" + GRAPH, 1)
    text = text.replace("\ntasks:\n", "\ntasks:\n  recap: { agendas: agendas }\n", 1)
    cfg.write_text(text)
    return ws / "Syncs" / "Platform.md"


def start(cli, ws, now, *extra):
    code, env = cli("run", "start", "agendas", *extra, "--now", now, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["run"]


def test_a_run_rolls_the_agenda_of_each_forum_that_met(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    start(cli, ws, pinned_now)
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    tasks = [t for s in res.steps if s["kind"] == "dispatch" for t in s["tasks"]]
    assert [t["kind"] for t in tasks] == ["recap"]                         # one meeting had a record and a forum
    pack = json.loads(open(tasks[0]["pack_path"]).read())
    (item,) = pack["items"]
    assert item["id"].endswith("m1") or item["title"] == "Platform Sync"
    assert item["fields"]["agenda"] == [{"id": "N1", "text": "- Capacity plan"}, {"id": "N2", "text": "- Hiring"},
                                        {"id": "N3", "text": "- Offsite"}]
    text = note.read_text()
    head, older = text.split("## 2026-10-08")
    # the synthetic recap says one thing was covered and that it settles N2
    assert "## 2026-10-14 *(no record, agenda assumed covered)*\n\n- Capacity plan\n- Offsite\n" in head
    assert "- Hiring\n" not in head.split("## 2026-10-14")[0] and head.startswith("# Platform Sync\n\n## Next\n\n## 2026-10-14")
    assert "→ [notes](https://docs.example.org/m1)" in older and older.rstrip().endswith("- Earlier topic")
    assert "Lunch" not in text

    start(cli, ws, pinned_now, "--new")
    again = run_agent(ws)
    assert again.failures == [] and again.final["kind"] == "done", again.final
    assert [s for s in again.steps if s["kind"] == "dispatch"] == []
    assert note.read_text() == text                                         # nothing is rolled twice


def test_a_roll_that_changes_what_its_upstream_reads_is_written_once(persona, cli, pinned_now):
    """The roll moves the agenda's latest dated section past the meetings, so afterwards the
    recap judge would find no meeting newer than the note and hand on something else. Once the
    write has begun, recap is read from its pin: the roll is offered once and the run seals."""
    ws = persona("reference-like")
    note = setup(ws)
    start(cli, ws, pinned_now)
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    assert res.final["summary"]["status"] == "sealed"
    rolls = [s for s in res.steps if s["node"] == "roll"]
    assert [(s["kind"], s["command"]["argv"][1:3]) for s in rolls] == [("run", ["do", "roll"])]
    assert sum(1 for argv in res.calls if argv[:2] == ["do", "roll"]) == 1
    assert [s["node"] for s in res.steps].count("recap") == 1               # the recap is not judged again
    assert res.steps[-1]["node"] == "seal"
    text = note.read_text()
    assert text != AGENDA
    assert text.count("## 2026-10-14") == 1 and text.count("## 2026-10-08") == 1
    assert res.final["summary"]["operations"] == res.final["summary"]["applied"] == 2  # two dated sections, once each
