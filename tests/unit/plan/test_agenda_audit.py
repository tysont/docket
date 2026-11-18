"""The agenda audit (TD §9.7, §10.7, §12.8): which forums a judge: audit node audits, what a
forum's pack item holds, the rules an audit row keeps, the edits an answer makes and how the
agenda takes them. All names and data are invented."""
from __future__ import annotations

import json
import os
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from docket.cli.errors import DocketError, InputError
from docket.config import load_workspace
from docket.engine.context import RunContext
from docket.items import ledger
from docket.plan import agenda_audit
from docket.decide import audit as qa
from docket.store.paths import state_root
from docket.store.runs import Store, load_run
from docket.tasks import submit
from docket.tasks.kinds import registry

NOW = "2026-10-15T18:00:00+00:00"   # a Thursday

WS = """\
version: 1
profile:
  timezone: UTC
  me: { name: Pat Operator, handles: { mail: pat@example.org } }
  people:
    - { id: lin, name: Lin Doe, about: "Runs the storage team.", reports_to: me, roles: [report], handles: { mail: lin@example.org } }
    - { id: ash, name: Ash Roe, reports_to: lin, handles: { mail: ash@example.org } }
    - { id: kai, name: Kai Moe, handles: { mail: kai@example.org } }
  topics:
    - { id: storage, area: Storage, forum: lin-weekly }
  forums:
    - { id: lin-weekly, name: Lin weekly, person: lin, agenda: { destination: agendas, path: notes/lin.md }, calendar_match: { attendee: lin@example.org } }
    - { id: ash-weekly, name: Ash weekly, person: ash, agenda: { destination: agendas, path: notes/ash.md }, calendar_match: { title_regex: "^Ash / Pat" } }
    - { id: kai-weekly, name: Kai weekly, person: kai, agenda: { destination: agendas, path: notes/kai.md }, calendar_match: { attendee: kai@example.org } }
    - { id: team, name: Team sync, agenda: { destination: agendas, path: notes/team.md }, calendar_match: { title_regex: "^Team sync" } }
sources:
  meetings:
    kind: files
    path: meetings.jsonl
    mapping: { id: "$.id", title: "$.title", text: "$.text", at: "$.at", fields.attendees: "$.who" }
destinations:
  agendas: { adapter: markdown_agenda, path: notes, section: "## Next", capacity: 4 }
planning:
  calendar: { provider: json_file, path: cal.json }
tasks:
  audit: { agendas: agendas, max_ledger: 2, sections: 2, section_chars: 120 }
graphs:
  day:
    read-meetings: { read: meetings }
    audit-agendas: { judge: audit, from: [read-meetings], audit: AUDIT }
    review: { ask: default, questions: { kinds: [audit], audit: { from: audit-agendas, to: agendas } } }
    update: { write: audit, to: agendas, from: [review] }
"""

LIN = """# Lin weekly

## Next

- Storage budget (https://chat.example.org/t/1)
  - two options
- Weekly status
- Hiring loop

## 2026-10-08

- Covered the roadmap. """ + "word " * 60 + """

## 2026-10-01

- Earlier topic

## 2026-09-24

- Oldest topic
"""


def workspace(tmp_path, cli, audit="{ after_meeting: true }", calendar=None, meetings=(), name="ws", planning=True):
    root = tmp_path / name
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "lin.md").write_text(LIN)
    (root / "notes" / "ash.md").write_text("# Ash weekly\n\n## Next\n\n- Career goals\n")
    (root / "notes" / "team.md").write_text("# Team sync\n\n## Next\n\n- Roadmap\n")
    (root / "meetings.jsonl").write_text("".join(json.dumps(m) + "\n" for m in meetings))
    (root / "cal.json").write_text(json.dumps({"timezone": "UTC", "events": calendar or []}))
    text = WS.replace("AUDIT", audit)
    if not planning:
        text = text.replace("planning:\n  calendar: { provider: json_file, path: cal.json }\n", "")
    (root / "docket.yaml").write_text(text)
    code, env = cli("run", "start", "day", "--now", NOW, "--workspace", str(root))
    assert code == 0, env
    return root, env["data"]["run"]


def context(root, run_id):
    return RunContext(load_workspace(root), load_run(Store(state_root(root, os.environ)), run_id), os.environ)


def fake_roll(rc, path, outcome="applied", run=None):
    """A roll of ``path`` as a writing node leaves it in a run's directory: its plan and receipt."""
    run = run or rc.run
    op = {"op_id": "op_roll_" + path.replace("/", "_"), "destination": "agendas", "verb": "roll", "path": path,
          "anchor": "## Next", "title": "## 2026-10-14", "body": "- covered"}
    pdir = run.dir / "plan" / op["op_id"]
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plan.json").write_text(json.dumps({"operations": [op]}))
    with run.paths.receipts.open("a") as fh:
        fh.write(json.dumps({"op_id": op["op_id"], "destination": "agendas", "outcome": outcome}) + "\n")


def selected(rc, items=()):
    sel = agenda_audit.selection(rc, "audit-agendas", list(items))
    return sel["selected"], sel


# --- which forums ---------------------------------------------------------------------


def test_after_meeting_audits_a_forum_rolled_in_this_run_only_when_it_has_a_person(tmp_path, cli):
    root, run_id = workspace(tmp_path, cli)
    rc = context(root, run_id)
    assert selected(rc)[0] == {}
    fake_roll(rc, "notes/lin.md")
    fake_roll(rc, "notes/team.md")          # a group forum: not audited by itself
    fake_roll(rc, "notes/ash.md", outcome="already_present")
    assert selected(rc)[0] == {"lin-weekly": ["after_meeting"]}


def test_after_meeting_counts_rolls_of_runs_since_the_last_sealed_live_run(tmp_path, cli):
    root, first = workspace(tmp_path, cli)
    rc1 = context(root, first)
    fake_roll(rc1, "notes/lin.md")
    code, env = cli("run", "abandon", first, "--workspace", str(root))
    assert code == 0, env
    code, env = cli("run", "start", "day", "--now", NOW, "--new", "--workspace", str(root))
    second = env["data"]["run"]
    assert selected(context(root, second))[0] == {"lin-weekly": ["after_meeting"]}   # abandoned after the last seal
    db = Store(state_root(root, os.environ)).db
    with db:
        db.execute("UPDATE runs SET status='sealed' WHERE run_id=?", (first,))
    assert selected(context(root, second))[0] == {}                                 # that run was sealed: done with


def test_before_meeting_counts_working_time_and_reads_the_planning_calendar_and_the_meetings(tmp_path, cli):
    cal = [{"id": "e1", "title": "Ash / Pat", "start": "2026-10-19T10:00:00Z", "end": "2026-10-19T10:30:00Z",
            "attendees": 2},                                                   # Monday: within two working days
           {"id": "e2", "title": "Team sync", "start": "2026-10-16T10:00:00Z", "end": "2026-10-16T11:00:00Z"}]
    meetings = [{"id": "m1", "title": "Lin / Pat", "at": "2026-10-21T09:00:00Z",
                 "who": ["pat@example.org", "lin@example.org"], "text": ""},  # Wednesday: past the horizon
                {"id": "m0", "title": "Lin / Pat", "at": "2026-10-08T09:00:00Z",
                 "who": ["pat@example.org", "lin@example.org"], "text": "held"}]
    root, run_id = workspace(tmp_path, cli, audit="{ after_meeting: false, before_meeting: 2d }", calendar=cal)
    rc = context(root, run_id)
    sel, full = selected(rc, [{**m, "fields": {"attendees": m["who"]}} for m in meetings])
    assert sel == {"ash-weekly": ["before_meeting"]}
    (item,) = full["candidates"]
    assert item["fields"]["meetings"] == {"last": None, "next": "2026-10-19T10:00:00+00:00"}
    lin = agenda_audit.selection(rc, "audit-agendas", [{**m, "fields": {"attendees": m["who"]}} for m in meetings])
    assert "lin-weekly" not in lin["selected"]


def test_a_before_meeting_audit_says_when_upcoming_events_are_unavailable(tmp_path, cli, monkeypatch):
    from docket.plan import build
    from docket.shapes.unexpected import collecting
    root, run_id = workspace(tmp_path, cli, audit="{ after_meeting: false, before_meeting: 2d }")

    def broken(rc, tz):
        raise OSError("calendar down")

    monkeypatch.setattr(build, "calendar_provider", broken)
    with collecting() as col:
        sel, full = selected(context(root, run_id))
    assert sel == {} and full["upcoming"].startswith("upcoming events unavailable: the planning calendar "
                                                     "could not be read (OSError)")
    assert [(x.area, x.detail) for x in col.peek()] == [("plan.audit", "upcoming events unavailable")]
    monkeypatch.undo()
    root, run_id = workspace(tmp_path, cli, audit="{ after_meeting: false, before_meeting: 2d }", name="ws2",
                             planning=False)
    with collecting():
        _, full = selected(context(root, run_id))
    assert "no planning calendar is configured" in full["upcoming"]


def test_named_forums_are_audited_and_a_forum_without_a_note_is_skipped(tmp_path, cli):
    root, run_id = workspace(tmp_path, cli, audit="{ after_meeting: false, forums: [team, kai-weekly] }")
    sel, full = selected(context(root, run_id))
    assert sel == {"team": ["named"]}
    assert full["skipped"] == [{"forum": "kai-weekly", "path": "notes/kai.md", "reason": "the note does not exist"}]


def test_working_time_skips_the_weekend():
    tz = ZoneInfo("UTC")
    fri = datetime(2026, 10, 16, 9, 0, tzinfo=tz)
    days = ["mon", "tue", "wed", "thu", "fri"]
    assert agenda_audit.working_until(fri, 2 * 1440, days) == datetime(2026, 10, 20, 9, 0, tzinfo=tz)
    assert agenda_audit.working_until(fri, 60, days) == datetime(2026, 10, 16, 10, 0, tzinfo=tz)
    sat = datetime(2026, 10, 17, 12, 0, tzinfo=tz)
    assert agenda_audit.working_until(sat, 1440, days) == datetime(2026, 10, 20, 0, 0, tzinfo=tz)


# --- the pack item ----------------------------------------------------------------------


def seed(root, rows):
    db = Store(state_root(root, os.environ)).db
    with db:
        for i, r in enumerate(rows):
            ledger.put(db, {"key": ledger.item_key(r["topic"]), "first_run": "seed", "first_at": "2026-10-01",
                            "last_run": "seed", "source": "chat", "state_at": f"2026-10-0{i + 1}T09:00:00Z",
                            "placements": [], "history": [], **r})


def test_a_forum_pack_item_holds_the_person_the_entries_the_dated_sections_and_the_related_items(tmp_path, cli):
    root, run_id = workspace(tmp_path, cli, audit="{ forums: [lin-weekly] }")
    seed(root, [
        {"topic": "a", "state": "done", "title": "Old message words", "ask": "Approve the storage budget",
         "url": "https://chat.example.org/t/1", "owner": "lin"},
        {"topic": "b", "state": "raised", "title": "Budget", "placements": [{"path": "notes/lin.md",
                                                                             "destination": "agendas"}]},
        {"topic": "c", "state": "backlog", "title": "Area item", "area": "Storage"},
        {"topic": "d", "state": "new", "title": "Unrelated", "owner": "kai"},
        {"topic": "e", "state": "backlog", "title": "Routed here", "forum": "lin-weekly", "sender": "ash"},
    ])
    rc = context(root, run_id)
    (item,) = selected(rc)[1]["candidates"]
    f = item["fields"]
    assert item["id"] == "forum:lin-weekly" and item["title"] == "Lin weekly"
    assert f["person"] == {"id": "lin", "name": "Lin Doe", "about": "Runs the storage team.", "roles": ["report"],
                           "reports_to": {"id": "me", "name": "Pat Operator"}}
    assert f["entries"] == [{"line": "N1", "text": "- Storage budget (https://chat.example.org/t/1)",
                             "lines": ["  - two options"]},
                            {"line": "N2", "text": "- Weekly status"}, {"line": "N3", "text": "- Hiring loop"}]
    assert [s["date"] for s in f["sections"]] == ["2026-10-08", "2026-10-01"]        # the latest two
    assert f["sections"][0].get("cut") is True and len(f["sections"][0]["text"]) <= 120
    assert f["meetings"]["last"] == "2026-10-08"
    rows = f["ledger"]
    assert rows["total"] == 4 and rows["shown"] == 2 and "2 older item(s) not shown" in rows["note"]
    assert [(r["ask"], r["why"]) for r in rows["items"]] == [("Routed here", ["forum"]), ("Area item", ["area"])]


def test_the_pack_item_shows_the_ask_of_an_item_and_not_its_message(tmp_path, cli):
    root, run_id = workspace(tmp_path, cli, audit="{ forums: [lin-weekly] }")
    seed(root, [{"topic": "a", "state": "done", "title": "Hey, quick thing about", "ask": "Approve the budget",
                 "owner": "lin", "url": "https://chat.example.org/t/1"}])
    (item,) = selected(context(root, run_id))[1]["candidates"]
    (row,) = item["fields"]["ledger"]["items"]
    assert row == {"key": ledger.item_key("a"), "state": "done", "date": "2026-10-01", "ask": "Approve the budget",
                   "link": "https://chat.example.org/t/1", "owner": "lin", "from": "", "why": ["owner"]}


# --- the row ------------------------------------------------------------------------------

ITEM = {"id": "forum:lin-weekly", "source": "audit", "title": "Lin weekly", "lines": [{"line": "L1", "text": "Lin"}],
        "fields": {"entries": [{"line": "N1", "text": "- Storage budget (https://chat.example.org/t/1)"},
                               {"line": "N2", "text": "- Weekly status"}],
                   "person": {"about": "Runs the storage team."}}}


def validate(row, max_add=3):
    from docket.config.task_models import TasksConfig
    kind = registry(TasksConfig.model_validate({"audit": {"max_add": max_add}}))["audit"]
    body = {"id": ITEM["id"], "add": [], "commitments": [], "focus": "Decide the budget", **row}
    return submit.validate(kind, {"items": [ITEM]}, {"items": [body]}, raw_text=lambda _i: None, rerun="rerun")


def codes(fn):
    with pytest.raises(DocketError) as ei:
        fn()
    return ei.value.code, [(r["path"], r["code"]) for r in (ei.value.details or {}).get("rows", [])]


BOTH = [{"line": "N1", "verdict": "keep"}, {"line": "N2", "verdict": "drop", "why": "A status check"}]


def test_a_valid_audit_row_is_accepted():
    ok = validate({"entries": BOTH, "add": [{"text": "Plan the storage hire", "why": "A risk Lin carries",
                                             "link": "https://chat.example.org/t/1",
                                             "evidence": "runs the  storage team"}],
                   "commitments": [{"who": "me", "what": "Send the budget", "status": "maybe"}],
                   "unknown": "whether the budget was approved"})
    (row,) = ok.body["items"]
    assert row["commitments"][0]["status"] == "unknown"        # an unknown status reads as unknown
    assert row["unknown"] == ["whether the budget was approved"]


@pytest.mark.parametrize("entries,expect", [
    ([{"line": "N1", "verdict": "keep"}], [("items[0].entries", "task.audit_entries")]),
    (BOTH + [{"line": "N2", "verdict": "keep"}], [("items[0].entries[2].line", "task.audit_entries")]),
    (BOTH + [{"line": "N9", "verdict": "keep"}], [("items[0].entries[2].line", "task.audit_entries")]),
    ([{"line": "N1", "verdict": "reword"}, BOTH[1]], [("items[0].entries[0].text", "task.cross_field")]),
])
def test_every_entry_is_answered_exactly_once_and_a_reword_says_its_words(entries, expect):
    code, rows = codes(lambda: validate({"entries": entries}))
    assert code == "task.rows_refused" and rows == expect


def test_an_addition_links_only_to_the_pack_and_quotes_only_the_pack():
    add = [{"text": "A topic", "why": "It matters", "link": "https://chat.example.org/t/2", "evidence": "words nowhere"}]
    code, rows = codes(lambda: validate({"entries": BOTH, "add": add}))
    assert rows == [("items[0].add[0].link", "task.unknown_link"), ("items[0].add[0].evidence",
                                                                    "task.evidence_not_verbatim")]


def test_additions_are_capped_by_max_add():
    add = [{"text": f"Topic {i}", "why": "It matters"} for i in range(3)]
    with pytest.raises(InputError) as ei:
        validate({"entries": BOTH, "add": add}, max_add=2)
    assert ei.value.code == "task.invalid_result"
    assert "keep at most 2 entries in `add`" in ei.value.details["errors"][0]["fix"]


# --- answers and edits ----------------------------------------------------------------------

AUDIT = {"forum": "lin-weekly", "forum_name": "Lin weekly", "path": "notes/lin.md", "digest": "d1",
         "person": {"id": "lin", "name": "Lin Doe"},
         "entries": [{"line": "N1", "text": "- Storage budget (https://chat.example.org/t/1)"},
                     {"line": "N2", "text": "- Weekly status"}, {"line": "N3", "text": "- Hiring loop"}],
         "ledger": [{"key": "iaaaaaaaa", "link": "https://chat.example.org/t/5", "state": "backlog"}],
         "row": {"entries": [{"line": "N1", "verdict": "reword", "text": "- Decide the storage budget"},
                             {"line": "N2", "verdict": "drop", "why": "A status check"},
                             {"line": "N3", "verdict": "keep"}],
                 "add": [{"text": "Plan the storage hire", "why": "A risk", "link": "https://chat.example.org/t/5"},
                         {"text": "Check team load", "why": "Health"}, {"text": "Third", "why": "Less"}],
                 "commitments": [], "focus": "Decide the budget"}}


def row_of(audit=AUDIT, cap=None):
    return {"question_id": qa.question_id(audit), "forum": audit["forum"], "forum_name": audit["forum_name"],
            "path": audit["path"], "destination": "agendas", **qa.proposal(audit, cap)}


def test_the_proposal_numbers_additions_names_their_ledger_items_and_lists_what_the_agenda_would_hold():
    p = qa.proposal(AUDIT, 4)
    assert [(e["line"], e["verdict"], e.get("new")) for e in p["entries"]] == [
        ("N1", "reword", "Decide the storage budget"), ("N2", "drop", None), ("N3", "keep", None)]
    assert [(a["id"], a.get("key")) for a in p["additions"]] == [("A1", "iaaaaaaaa"), ("A2", None), ("A3", None)]
    assert p["proposed"] == ["- Decide the storage budget", "- Hiring loop", "- Plan the storage hire",
                             "- Check team load"]                       # four fit the agenda


@pytest.mark.parametrize("option,overrides,expect", [
    ("accept", {}, (["N1"], ["N2"], ["A1", "A2", "A3"])),
    ("accept", {"keep": ["N2"], "without": ["A3"]}, (["N1"], [], ["A1", "A2"])),
    ("additions", {"without": ["A1"]}, ([], [], ["A2", "A3"])),
    ("skip", {}, ([], [], [])),
])
def test_each_option_and_override_makes_its_changes(option, overrides, expect):
    rw, drops, adds = qa.changes(row_of(), {"option": option, "overrides": overrides})
    assert ([e["line"] for e in rw], [e["line"] for e in drops], [a["id"] for a in adds]) == expect


@pytest.mark.parametrize("option,overrides,why", [
    ("accept", {"keep": ["N3"]}, "N3 is kept already"),
    ("accept", {"without": ["A9"]}, "no such addition"),
    ("additions", {"keep": ["N1"]}, "additions leaves the entries alone"),
    ("skip", {"without": ["A1"]}, "skip changes nothing"),
])
def test_overrides_name_only_changes_the_option_makes(option, overrides, why):
    q = {"id": "u_1", "kind": "audit", "audit": row_of()}
    with pytest.raises(InputError) as ei:
        qa.check_overrides(q, option, overrides)
    assert ei.value.code == "input.bad_override", why


def test_override_ids_parse_from_lists_and_commas():
    assert qa.parse_ids(["n2,N5", "N2"], "N", "--keep") == ["N2", "N5"]
    with pytest.raises(InputError):
        qa.parse_ids("2", "N", "--keep")


def test_a_skip_and_an_answer_that_leaves_every_change_out_are_remembered():
    r = row_of()
    decide = {"audit": [r], "answers": {r["question_id"]: {"option": "skip"}}}
    assert [d.decision for d in qa.dispositions("run", decide)] == ["skip"]
    decide["answers"][r["question_id"]] = {"option": "accept", "overrides": {"keep": ["N1", "N2"],
                                                                             "without": ["A1", "A2", "A3"]}}
    assert [d.topic_id for d in qa.dispositions("run", decide)] == [r["question_id"]]
    decide["answers"][r["question_id"]] = {"option": "accept"}
    assert qa.dispositions("run", decide) == []


def test_a_proposal_that_changes_nothing_is_not_asked(tmp_path, cli):
    quiet = {**AUDIT, "row": {"entries": [{"line": n, "verdict": "keep"} for n in ("N1", "N2", "N3")], "add": [],
                              "commitments": [], "focus": "Same"}}
    assert not any(qa.counts(qa.proposal(quiet)).values())


def test_writes_rewords_and_drops_first_then_the_additions_that_fit(tmp_path, cli):
    root, run_id = workspace(tmp_path, cli)
    rc = context(root, run_id)
    r = row_of(cap=4)
    built = agenda_audit.write_operations(rc, "agendas", {"audit": [r], "answers": {r["question_id"]: {
        "option": "accept"}}})
    ops = built["operations"]
    assert [(o["verb"], o.get("old_line"), o.get("line")) for o in ops] == [
        ("replace", "- Storage budget (https://chat.example.org/t/1)",
         "- Decide the storage budget (https://chat.example.org/t/1)"),
        ("remove", "- Weekly status", None),
        ("append", None, "- Plan the storage hire [link](https://chat.example.org/t/5)"),
        ("append", None, "- Check team load")]
    assert ops[2]["item_id"] == "iaaaaaaaa"
    assert built["left_out"] == [{"forum": "lin-weekly", "path": "notes/lin.md", "id": "A3", "text": "Third",
                                  "reason": "the agenda holds 4 entries"}]
    assert built["refused"] == []


def test_an_entry_edited_since_the_proposal_is_refused_before_it_is_written(tmp_path, cli):
    root, run_id = workspace(tmp_path, cli)
    (root / "notes" / "lin.md").write_text(LIN.replace("- Weekly status", "- Weekly status, edited"))
    rc = context(root, run_id)
    r = row_of(cap=4)
    built = agenda_audit.write_operations(rc, "agendas", {"audit": [r], "answers": {r["question_id"]: {
        "option": "accept", "overrides": {"without": ["A1", "A2", "A3"]}}}})
    assert [o["verb"] for o in built["operations"]] == ["replace"]
    (bad,) = built["refused"]
    assert bad["verb"] == "remove" and bad["error"]["code"] == "dest.line_changed"


# --- the ledger -----------------------------------------------------------------------------


def test_the_ledger_raises_an_added_item_and_returns_a_dropped_one_to_its_earlier_state(tmp_path, cli):
    root, _ = workspace(tmp_path, cli)
    db = Store(state_root(root, os.environ)).db
    place = {"path": "notes/lin.md", "destination": "agendas", "adapter": "markdown_agenda"}
    seed(root, [
        {"topic": "add", "state": "backlog", "title": "Hire", "history": [{"state": "backlog"}]},
        {"topic": "drop", "state": "raised", "title": "Status", "placements": [{**place, "line": "- Weekly status"}],
         "history": [{"state": "monitoring"}, {"state": "raised"}]},
        {"topic": "drop2", "state": "raised", "title": "Other", "url": "https://chat.example.org/t/9",
         "placements": [{**place, "line": "- Other"}], "history": [{"state": "raised"}]},
        {"topic": "reword", "state": "raised", "title": "Budget", "placements": [{**place, "line": "- Budget"}]},
    ])
    edits = [{"verb": "append", "item_id": ledger.item_key("add"), "destination": "agendas",
              "adapter": "markdown_agenda", "op_id": "op_1", "path": "notes/lin.md", "anchor": "## Next",
              "line": "- Hire"},
             {"verb": "remove", "path": "notes/lin.md", "old_line": "- Weekly status"},
             {"verb": "remove", "path": "notes/lin.md", "old_line": "- Other, reworded (https://chat.example.org/t/9)"},
             {"verb": "replace", "path": "notes/lin.md", "old_line": "- Budget", "line": "- Decide the budget"}]
    with db:
        moves = ledger.audited(db, edits, run_id="r1", at="2026-10-15T18:00:00Z",
                               forums={"notes/lin.md": "lin-weekly"})
    assert {(m["key"], m["state"]) for m in moves} == {(ledger.item_key("add"), "raised"),
                                                       (ledger.item_key("drop"), "monitoring"),
                                                       (ledger.item_key("drop2"), "acknowledged")}
    added = ledger.get(db, "add")
    assert added["state"] == "raised" and added["placements"][-1]["forum"] == "lin-weekly"
    assert added["history"][-1]["note"] == "added to notes/lin.md (lin-weekly) by the agenda audit"
    assert ledger.get(db, "drop")["history"][-1]["decision"] == "dropped"
    rw = ledger.get(db, "reword")
    assert rw["state"] == "raised" and rw["placements"][0]["line"] == "- Decide the budget"
