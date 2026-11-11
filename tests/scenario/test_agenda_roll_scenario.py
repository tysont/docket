"""Agendas are rolled after the meetings held (TD §12.8): a run collects meetings, the recap
task reads the recorded ones beside the forum's agenda, and apply writes the dated sections."""
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


def setup(ws):
    (ws / "Syncs").mkdir(exist_ok=True)
    (ws / "Syncs" / "Platform.md").write_text(AGENDA)
    rows = [
        {"id": "m1", "title": "Platform Sync", "at": "2026-10-08T15:00:00Z", "who": ["sam@team.example", "kim@team.example"],
         "text": "Capacity: the plan was agreed.\nA new topic came up: the vendor review.", "doc": "https://docs.example.org/m1"},
        {"id": "m2", "title": "Platform Sync", "at": "2026-10-14T15:00:00Z", "who": ["sam@team.example", "kim@team.example"],
         "text": ""},
        {"id": "m3", "title": "Lunch and learn", "at": "2026-10-13T18:00:00Z", "who": ["x@team.example", "y@team.example"],
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
    assert "\nworkflows:\n" in text
    text = text.replace("\nworkflows:\n", """
workflows:
  agendas:
    description: Roll agendas after the meetings held.
    stages: [collect, assess, merge, decide, plan, approve, apply, close]
    sources: [meetings]
    roll: { sources: [meetings], destination: agendas, no_record: "no record, agenda assumed covered", trailer: "→ [notes]({url})" }
    questions: { kinds: [], budget: 0 }
""", 1)
    assert "\ntasks:\n" in text
    text = text.replace("\ntasks:\n", "\ntasks:\n  recap:\n    select: { sources: [meetings] }\n", 1)
    cfg.write_text(text)
    return ws / "Syncs" / "Platform.md"


def test_a_run_rolls_the_agenda_of_each_forum_that_met(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    code, env = cli("run", "start", "agendas", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
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

    code, env = cli("run", "start", "agendas", "--new", "--now", pinned_now, "--workspace", str(ws))
    again = run_agent(ws)
    assert again.failures == [] and [s for s in again.steps if s["kind"] == "dispatch"] == []
    assert note.read_text() == text                                         # nothing is rolled twice
