"""An agenda audit at the end of a run (TD §9.7, §10.7, §12.8): the run reads the meetings, rolls
the note of the forum that met, applies an approved plan, audits the rolled forum's agenda, asks
the operator about the proposal and writes the answer. All names and data are invented."""
from __future__ import annotations

import json
import os
import sqlite3

from docket.items import ledger
from docket.store.paths import state_root
from tests.agent_loop import run_agent

NOTE = """# Sam / Dana

## Next

- Weekly status of the ingest migration
- Hiring plan for the new role
  - two candidates in the loop
- Offsite logistics (https://chat.example.org/t/7)

## 2026-10-01

- Earlier topic
"""

AFTER = """# Sam / Dana

## Next

- Decide who runs the offsite (https://chat.example.org/t/7)
- Decide the on-call rotation for ingest (https://chat.example.org/t/42)

## 2026-10-14

- Hiring: two offers out
- Action items:
\t- Sam to send the offer letters

→ [notes](https://docs.example.org/m1)

## 2026-10-01

- Earlier topic
"""

GRAPH = """
  agendas:
    read-meetings: { read: meetings }
    recap: { judge: recap, from: [read-meetings] }
    roll-agendas: { write: roll, from: [recap], to: agendas, trailer: "→ [notes]({url})" }
    decide: { ask: basic, from: [recap] }
    plan: { plan: [decide] }
    approve: { approve: plan }
    apply-writes: { apply: approve }
    audit-agendas: { judge: audit, from: [read-meetings], after: [roll-agendas, apply-writes], audit: { after_meeting: true, before_meeting: 2d } }
    review-agendas: { ask: default, questions: { kinds: [audit], audit: { from: audit-agendas, to: agendas } } }
    update-agendas: { write: audit, to: agendas, from: [review-agendas] }
"""

FORUM = """    - id: sam-one-on-one
      name: Sam / Dana
      kind: one_on_one
      person: sam
      agenda: { destination: agendas, path: "Syncs/Sam.md" }
      calendar_match: { attendee: sam@northwind.example }
"""

MEETINGS = [
    {"id": "m1", "title": "Sam / Dana", "at": "2026-10-14T15:00:00Z", "who": ["dana@northwind.example", "sam@northwind.example"],
     "text": "Sam raised the on-call load on the ingest team.\nHiring: two offers are out.", "doc": "https://docs.example.org/m1"},
]

LEDGER = [  # key, state, url, ask, owner, sender, forum, placement line, history states
    ("status", "raised", None, "Raise the weekly status", "me", None, "sam-one-on-one",
     "- Weekly status of the ingest migration", ["monitoring", "raised"]),
    ("offsite", "raised", "https://chat.example.org/t/7", "Settle the offsite logistics", "me", "sam", None,
     "- Offsite logistics (https://chat.example.org/t/7)", ["raised"]),
    ("oncall", "backlog", "https://chat.example.org/t/42", "Decide the on-call rotation for ingest", "sam", None, None,
     None, ["backlog"]),
]


def setup(ws):
    (ws / "Syncs").mkdir(exist_ok=True)
    (ws / "Syncs" / "Sam.md").write_text(NOTE)
    (ws / "data" / "meetings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in MEETINGS))
    src = ws / "sources.yaml"
    src.write_text(src.read_text() + """  meetings:
    kind: files
    description: Meetings held, each with its record.
    path: data/meetings.jsonl
    mapping: { id: "$.id", title: "$.title", text: "$.text", at: "$.at", fields.attendees: "$.who", fields.record_url: "$.doc" }
""")
    prof = ws / "profile.yaml"
    text = prof.read_text()
    text = text.replace("      roles: [report]\n    - id: ola", "      roles: [report]\n      about: Leads the ingest team and owns "
                        "its on-call rotation.\n    - id: ola", 1)
    prof.write_text(text.replace("  calendar:\n    classify:", FORUM + "  calendar:\n    classify:", 1))
    cfg = ws / "docket.yaml"
    text = cfg.read_text()
    text = text.replace("\ngraphs:\n", "\ngraphs:" + GRAPH, 1)
    text = text.replace("\ntasks:\n", "\ntasks:\n  recap: { agendas: agendas }\n  audit: { agendas: agendas }\n", 1)
    cfg.write_text(text)
    return ws / "Syncs" / "Sam.md"


def seed_ledger(ws):
    path = state_root(ws, os.environ) / "docket.db"
    from docket.store.db import open_db
    db = open_db(path)
    with db:
        for key, state, url, ask, owner, sender, forum, line, states in LEDGER:
            placements = [{"destination": "agendas", "adapter": "markdown_agenda", "path": "Syncs/Sam.md",
                           "anchor": "## Next", "line": line, "run": "seed"}] if line else []
            ledger.put(db, {"key": ledger.item_key(key), "topic": key, "source": "chat", "title": ask, "url": url,
                            "state": state, "state_at": "2026-10-10T09:00:00-05:00", "first_run": "seed",
                            "first_at": "2026-10-09T09:00:00-05:00", "last_run": "seed", "placements": placements,
                            "history": [{"run": "seed", "decision": "seed", "state": s} for s in states],
                            "ask": ask, "owner": owner, "sender": sender, "forum": forum})
    db.close()
    return path


def states(db_path):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        return {r["topic"]: (r["state"], json.loads(r["placements"])) for r in db.execute("SELECT * FROM item_ledger")}
    finally:
        db.close()


def audit_result(kind, pack, prompt):
    if kind != "audit":
        return None
    (item,) = pack["items"]
    lines = [e["line"] for e in item["fields"]["entries"]]
    if lines != ["N1", "N2"]:   # the note before its roll: drop the first entry, keep the rest
        return {"items": [{"id": item["id"], "add": [], "commitments": [], "focus": "Keep the agenda short",
                           "entries": [{"line": n, "verdict": "drop" if n == "N1" else "keep"} for n in lines]}]}
    return {"items": [{
        "id": item["id"],
        "entries": [{"line": "N1", "verdict": "drop", "why": "A status check belongs in a message"},
                    {"line": "N2", "verdict": "reword", "text": "Decide who runs the offsite",
                     "why": "A decision only the two of them can make"}],
        "add": [{"text": "Decide the on-call rotation for ingest", "why": "A risk Sam carries",
                 "link": "https://chat.example.org/t/42", "evidence": "owns its on-call rotation"},
                {"text": "Review the hiring plan", "why": "Sam owes it (guess)"}],
        "commitments": [{"who": "them", "what": "Send the offer letters", "status": "open"}],
        "focus": "Settle the on-call rotation and the offsite owner",
        "unknown": ["whether the offers were accepted"]}]}


def start(cli, ws, now, *extra):
    code, env = cli("run", "start", "agendas", *extra, "--now", now, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["run"]


def is_audit_ask(step):
    return step["kind"] == "ask" and step["node"] == "review-agendas"


def test_a_run_audits_the_forum_it_rolled_and_writes_what_the_operator_accepts(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    start(cli, ws, pinned_now)
    db_path = seed_ledger(ws)

    first = run_agent(ws, stop_at=is_audit_ask, full=True, results=audit_result)
    assert first.final.get("kind") == "ask", json.dumps(first.final)[:3000]
    tasks = [t for s in first.steps if s["kind"] == "dispatch" for t in s["tasks"]]
    assert [t["kind"] for t in tasks] == ["recap", "audit"]
    pack = json.loads(open(tasks[1]["pack_path"]).read())
    fields = pack["items"][0]["fields"]
    assert fields["forum"] == {"id": "sam-one-on-one", "name": "Sam / Dana", "path": "Syncs/Sam.md"}
    assert fields["person"]["about"] == "Leads the ingest team and owns its on-call rotation."
    assert fields["person"]["reports_to"] == {"id": "me", "name": "Dana Reyes"}
    assert fields["entries"] == [{"line": "N1", "text": "- Weekly status of the ingest migration"},
                                 {"line": "N2", "text": "- Offsite logistics (https://chat.example.org/t/7)"}]
    assert [s["date"] for s in fields["sections"]] == ["2026-10-14", "2026-10-01"]
    assert fields["meetings"] == {"last": "2026-10-14", "next": None}
    assert sorted(r["ask"] for r in fields["ledger"]["items"]) == [
        "Decide the on-call rotation for ingest", "Raise the weekly status", "Settle the offsite logistics"]

    (q,) = first.final["questions"]
    assert q["kind"] == "audit" and q["prompt"] == "Update the Sam / Dana agenda as its audit proposes " \
                                                  "(1 reworded, 1 dropped, 2 added)?"
    assert [o["id"] for o in q["options"]] == ["accept", "additions", "skip"]
    d = q["details"]["audit"]
    assert [(e["line"], e["verdict"]) for e in d["entries"]] == [("N1", "drop"), ("N2", "reword")]
    assert [a["id"] for a in d["additions"]] == ["A1", "A2"]
    assert d["proposed"] == ["- Decide who runs the offsite", "- Decide the on-call rotation for ingest",
                             "- Review the hiring plan"]
    assert d["focus"] and d["commitments"][0]["who"] == "them" and d["unknown"]

    code, env = cli("ask", "answer", q["id"], "accept", "--without", "A2", "--workspace", str(ws))
    assert code == 0, env
    assert env["data"]["action_preview"]["agenda_effect"] == {"reword": ["N2"], "drop": ["N1"], "add": ["A1"],
                                                               "path": "Syncs/Sam.md"}
    rest = run_agent(ws, results=audit_result)
    assert rest.failures == [] and rest.final["kind"] == "done", rest.final
    assert note.read_text() == AFTER

    got = states(db_path)
    assert got["status"][0] == "monitoring"                      # dropped: back to its state before it was raised
    assert got["offsite"][0] == "raised"                         # reworded: still raised, at its new line
    assert got["offsite"][1][0]["line"] == "- Decide who runs the offsite (https://chat.example.org/t/7)"
    assert got["oncall"][0] == "raised"                          # added from the ledger: raised at this forum
    assert got["oncall"][1][-1]["forum"] == "sam-one-on-one"
    assert got["oncall"][1][-1]["line"] == "- Decide the on-call rotation for ingest (https://chat.example.org/t/42)"

    start(cli, ws, pinned_now, "--new")
    again = run_agent(ws, results=audit_result)
    assert again.failures == [] and again.final["kind"] == "done", again.final
    assert [t["kind"] for s in again.steps if s["kind"] == "dispatch" for t in s["tasks"]] == []
    asked = [q for s in again.steps if s["kind"] == "ask" for q in s["questions"]]
    assert [q["kind"] for q in asked] == ["approve"]             # only the approval of an empty plan
    assert note.read_text() == AFTER


def test_a_sandbox_run_leaves_the_note_and_the_ledger_as_they_were(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    start(cli, ws, pinned_now, "--sandbox")
    db_path = seed_ledger(ws)
    before = states(db_path)
    res = run_agent(ws, results=audit_result)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    assert any(s["node"] == "review-agendas" for s in res.steps if s["kind"] == "ask")
    assert note.read_bytes() == NOTE.encode()
    assert states(db_path) == before


def test_an_entry_edited_since_the_proposal_holds_the_write(persona, cli, pinned_now, monkeypatch):
    """The note is edited after the write planned its edits and before it applies them: the
    reword's line no longer reads as the audit saw it, so the agenda refuses that edit by the old
    line's digest, the rest is written, and the node holds until it is repeated or accepted."""
    from docket.apply import executor
    ws = persona("reference-like")
    note = setup(ws)
    start(cli, ws, pinned_now)
    seed_ledger(ws)
    first = run_agent(ws, stop_at=is_audit_ask, results=audit_result)
    (q,) = first.final["questions"]
    code, env = cli("ask", "answer", q["id"], "accept", "--workspace", str(ws))
    assert code == 0, env
    real = executor.execute

    def edit_first(rc, ops):
        if any(o.get("verb") == "replace" for o in ops):
            note.write_text(note.read_text().replace("- Offsite logistics", "- Offsite logistics, edited"))
        return real(rc, ops)

    monkeypatch.setattr(executor, "execute", edit_first)
    res = run_agent(ws, results=audit_result)
    assert res.final["kind"] == "error" and res.final["error"]["error"]["code"] == "dest.apply_failed", res.final
    code, env = cli("next", "--workspace", str(ws))
    (wait,) = [s for s in env["data"]["steps"] if s["kind"] == "wait"]
    assert wait["reason"] == "apply_partial" and wait["node"] == "update-agendas"
    (op,) = wait["operations"]
    assert op["outcome"] == "refused" and op["code"] == "dest.line_changed" and op["verb"] == "replace"
    text = note.read_text()
    assert "- Offsite logistics, edited (https://chat.example.org/t/7)" in text
    assert "Weekly status" not in text and "- Decide the on-call rotation for ingest" in text   # the rest was written
    code, env = cli("do", "update-agendas", "--accept-failures", "--workspace", str(ws))
    assert code == 0, env
    rest = run_agent(ws, results=audit_result)
    assert rest.failures == [] and rest.final["kind"] == "done", rest.final


def test_an_unchanged_forum_reuses_its_audit_and_a_skip_is_remembered_until_the_note_changes(persona, cli, pinned_now):
    ws = persona("reference-like")
    note = setup(ws)
    (ws / "data" / "meetings.jsonl").write_text("")                 # no meeting: nothing rolls
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("audit: { after_meeting: true, before_meeting: 2d }",
                                           "audit: { after_meeting: false, forums: [sam-one-on-one] }"))
    start(cli, ws, pinned_now)
    seed_ledger(ws)

    def audits(res):
        return [t for s in res.steps if s["kind"] == "dispatch" for t in s["tasks"] if t["kind"] == "audit"]

    first = run_agent(ws, stop_at=is_audit_ask, results=audit_result)
    assert len(audits(first)) == 1
    (q,) = first.final["questions"]
    code, env = cli("ask", "answer", q["id"], "skip", "--workspace", str(ws))
    assert code == 0, env
    done = run_agent(ws, results=audit_result)
    assert done.final["kind"] == "done" and note.read_text() == NOTE, done.final

    start(cli, ws, pinned_now, "--new")
    again = run_agent(ws, results=audit_result)
    assert again.final["kind"] == "done", again.final
    assert audits(again) == []                                       # the same pack: its result is reused
    assert not any(s["node"] == "review-agendas" for s in again.steps if s["kind"] == "ask")   # the skip holds

    note.write_text(NOTE.replace("- Hiring plan for the new role", "- Hiring plan for the new roles"))
    start(cli, ws, pinned_now, "--new")
    changed = run_agent(ws, stop_at=is_audit_ask, results=audit_result)
    assert len(audits(changed)) == 1 and changed.final["kind"] == "ask"   # an edited note is audited again
