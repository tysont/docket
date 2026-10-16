"""Close units (TD §13.2, §13.4 to §13.7): cursors, ledgers, dispositions, journal rendering, change
summaries and configuration checks. All data is invented."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from docket.apply.freshness import summarize
from docket.close import cursors, dispositions, ledgers
from docket.close.journal import render
from docket.config.close_models import JournalConfig
from docket.store.db import open_db

pytestmark = pytest.mark.smoke


def _db(tmp_path: Path):
    return open_db(tmp_path / "docket.db")


def _run(run_id="r2", sandbox=False, status="open"):
    return SimpleNamespace(id=run_id, header=SimpleNamespace(sandbox=sandbox, status=status))


def _proposal(db, run, source, unit, pos, model="timestamp"):
    db.execute("INSERT INTO cursor_proposals (run_id, source, unit, position, coverage_end, generation, model, "
               "complete) VALUES (?,?,?,?,?,0,?,1)", (run, source, unit, pos, pos, model))


def test_gap_holds_while_others_advance_and_regressions_are_recorded(tmp_path):
    db = _db(tmp_path)
    with db:
        db.execute("INSERT INTO cursors VALUES ('chat','room-a','2026-10-14T10:00:00Z','2026-10-14T10:00:00Z','r1')")
        db.execute("INSERT INTO cursors VALUES ('mail','*','2026-10-14T12:00:00Z','2026-10-14T12:00:00Z','r1')")
        db.execute("INSERT INTO cursors VALUES ('wiki','*','2026-10-14T08:00:00Z','2026-10-14T08:00:00Z','r1')")
        # chat proved coverage only up to its gap; mail proved the full window; tickets proved nothing (held)
        _proposal(db, "r2", "chat", "room-a", "2026-10-15T07:00:00Z")
        _proposal(db, "r2", "mail", "*", "2026-10-15T18:00:00Z")
        _proposal(db, "r2", "wiki", "*", "2026-10-13T08:00:00Z")  # behind the durable cursor
    with db:
        moved = cursors.advance(db, _run())
    cur = {(r["source"], r["unit"]): r["position"] for r in db.execute("SELECT * FROM cursors")}
    assert cur[("chat", "room-a")] == "2026-10-15T07:00:00Z"   # forward to the gap, not past it
    assert cur[("mail", "*")] == "2026-10-15T18:00:00Z"
    assert cur[("wiki", "*")] == "2026-10-14T08:00:00Z"        # regression ignored
    assert moved["advanced"] == ["chat", "mail"] and moved["held"] == ["wiki"]
    led = ledgers.rows(db, "cursor_regressions")
    assert led[0]["proposed"] == "2026-10-13T08:00:00Z" and led[0]["key"] == "wiki/*"


@pytest.mark.parametrize("run", [_run(sandbox=True), _run(status="abandoned")])
def test_sandbox_and_abandoned_runs_advance_nothing(tmp_path, run):
    db = _db(tmp_path)
    with db:
        _proposal(db, "r2", "mail", "*", "2026-10-15T18:00:00Z")
    with db:
        moved = cursors.advance(db, run)
    assert moved["advanced"] == [] and moved["skipped"]
    assert db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0


def test_ledgers_compute_rows_and_keep_recent_runs(tmp_path):
    from docket.config.close_models import LedgerSpec
    spec = {"carries": LedgerSpec.model_validate({"from": "answers", "where": {"action": "watch"}, "key": "{topic}",
                                                  "fields": {"title": "{title}", "since": "{run.date}"}})}
    ctx = {"run": {"date": "2026-10-15"}, "answers": [
        {"topic": "ref:t:A-1", "title": "Watch the rollout", "action": "watch"},
        {"topic": "ref:t:A-2", "title": "Add a task", "action": "task.add"},
        {"topic": "ref:t:A-1", "title": "Duplicate key", "action": "watch"}]}
    entries = ledgers.compute(spec, ctx)
    assert entries == [("carries", "ref:t:A-1", {"since": "2026-10-15", "title": "Watch the rollout"})]
    db = _db(tmp_path)
    with db:
        for i in range(4):
            ledgers.append(db, "carries", f"r{i}", "k", {"n": i})
    assert [r["n"] for r in ledgers.rows(db, "carries", keep_runs=2)] == [2, 3]


def test_dispositions_by_topic_from_outcomes_answers_and_routes():
    item = {"id": "i1", "topic": "ref:ticket:T-100"}
    other = {"id": "i2", "topic": "thread:abc"}
    plain = {"id": "i3", "topic": ""}
    decide = {"findings": [{"question_id": "q_i1", "item": item}, {"question_id": "q_i2", "item": other},
                           {"question_id": "q_i3", "item": plain}],
              "answers": {"q_i1": {"action": "dismiss", "overrides": {"until": "2026-11-01"}},
                          "q_i2": {"action": "task.add"}, "q_i3": {"action": "now", "overrides": {"note": "called back"}}}}
    ops = [{"op_id": "op_1", "destination": "worklog", "question_id": "q_i2", "item_id": "i2"}]
    recs = {"op_1": {"outcome": "applied"}}
    rows = {d.topic_id: d for d in dispositions.derive("r1", decide, None, ops, recs)}
    assert rows["ref:ticket:T-100"].decision == "dismiss" and rows["ref:ticket:T-100"].until == "2026-11-01"
    assert rows["thread:abc"].decision == "routed" and rows["thread:abc"].op_id == "op_1"
    assert rows["i3"].decision == "now" and rows["i3"].outcome == "called back"   # no topic: the item id
    # the plan's outcomes list, when present, is the source of outcome decisions
    outs = [{"question": "q_i1", "item": "i1", "topic": "ref:ticket:T-100", "decision": "done", "note": "fixed"}]
    rows = {d.topic_id: d for d in dispositions.derive("r1", decide, outs, ops, recs)}
    assert rows["ref:ticket:T-100"].decision == "done" and set(rows) == {"ref:ticket:T-100", "thread:abc"}
    # an operation that did not apply routes nothing
    assert "thread:abc" not in {d.topic_id for d in dispositions.derive("r1", decide, outs, ops, {})}


def test_change_summaries_name_what_changed():
    assert summarize("calendar_file", {"owned": {}, "busy": [], "unowned_ids": ["a"]},
                     {"owned": {}, "busy": [], "unowned_ids": ["a", "b", "c"]}) == "2 calendar events added since build"
    assert summarize("markdown_tasks", {"text": "- [ ] a\n- [ ] b\n"}, {"text": "- [ ] a\n- [ ] c\n- [ ] d\n"}) == \
        "2 lines added, 1 line removed since build"
    assert summarize("markdown_agenda", {"files": {"x.md": "a"}}, {"files": {"x.md": "a", "y.md": "b"}}) == \
        "y.md: file added since build"


def test_journal_render_sections_levels_and_skip_empty():
    cfg = JournalConfig.model_validate({"title": "Log {run.date}", "sections": [
        {"heading": "Sources", "template": "{#sources}- {id}: {outcome}\n{/sources}"},
        {"heading": "Dropped", "template": "{#dropped}- {title}\n{/dropped}", "skip_empty": True}]})
    ctx = {"run": {"date": "2026-10-15"}, "sources": [{"id": "chat", "outcome": "read"}], "dropped": [],
           "approval": {"delay": ""}}
    assert render(cfg, ctx) == "# Log 2026-10-15\n\n## Sources\n- chat: read\n"
    assert render(cfg, ctx, level=3, title=False) == "### Sources\n- chat: read\n"


def _issues(tmp_path: Path, extra: str) -> list:
    from docket.config.loader import load
    from docket.config.validate import validate
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docket.yaml").write_text("version: 1\nprofile:\n  me: { name: Robin Vale }\nworkflows:\n"
                                    "  w:\n    stages: [collect, close]\n    hooks: { after_close: [nope] }\n"
                                    "destinations:\n  backlog: { adapter: markdown_tasks }\n" + extra)
    _, iss = validate(load(ws))
    return iss


def test_journal_and_hook_configuration_is_checked(tmp_path):
    iss = _issues(tmp_path, "journal:\n  destination: backlog\n  sections:\n"
                            "    - { heading: X, template: '{#findings}{secret_field}{/findings}' }\n"
                            "hooks:\n  h:\n    command: [prog, '{item.text}']\n    events: [after_close]\n"
                            "  bad:\n    command: [prog]\n    events: [before_lunch]\n")
    by_path = {i.path: i.code for i in iss}
    assert by_path["journal.destination"] == "config.bad_reference"
    assert by_path["journal.sections[0].template"] == "config.bad_template"
    assert by_path["hooks.h.command[1]"] == "config.bad_template"     # hook args see run facts only
    assert by_path["workflows.w.hooks.after_close[0]"] == "config.bad_reference"
    assert any(p.startswith("hooks.bad.events") for p in by_path)


def test_step_receipt_round_trip(tmp_path):
    from docket.close import receipts
    db = _db(tmp_path)
    with db:
        receipts.put(db, "r1", "hook:after_close:h", "failed", {"exit": 3})
    assert receipts.done(db, "r1", "hook:after_close:h") is None      # a failed step is retried
    with db:
        receipts.put(db, "r1", "hook:after_close:h", "warned", {"exit": 3})
    assert receipts.done(db, "r1", "hook:after_close:h")["exit"] == 3
    assert json.loads(json.dumps(receipts.for_run(db, "r1")))[0]["step"] == "hook:after_close:h"


def test_merge_reads_dispositions_by_topic_for_routed_and_now():
    """A new item id under a decided topic meets the disposition (TD §7.3, §13.7)."""
    from docket.signals.merge import MergeInputs, build_merge
    from tests.signal_helpers import env, item
    items = [item("new-quiet", text="again about INC-71", at="2026-10-14T10:00:00+00:00"),
             item("new-active", text="again about INC-72", at="2026-10-15T10:00:00+00:00"),
             item("handled", text="again about INC-73", at="2026-10-14T09:00:00+00:00")]
    rows = [{"topic_id": "ref:incident:INC-71", "decision": "routed", "run_id": "r1", "item": "old-1",
             "destination": "backlog", "op_id": "op_1", "recorded_at": "2026-10-14T18:00:00+00:00"},
            {"topic_id": "ref:incident:INC-72", "decision": "routed", "run_id": "r1", "item": "old-2",
             "destination": "backlog", "op_id": "op_2", "recorded_at": "2026-10-14T18:00:00+00:00"},
            {"topic_id": "ref:incident:INC-73", "decision": "now", "run_id": "r1", "item": "old-3",
             "outcome": "called back", "recorded_at": "2026-10-14T18:00:00+00:00"}]
    data = build_merge(MergeInputs(items=items, env=env([], references={"incident": "\\bINC-\\d+\\b"}),
                                   dispositions=rows, run_date="2026-10-15", run_id="r2"))
    by = {it["id"]: it for it in data["items"]}
    assert by["new-quiet"]["status"] == "suppressed"
    assert by["new-quiet"]["reason"] == "disposition: routed to backlog, no new activity"
    assert by["new-quiet"]["suppressed"]["op_id"] == "op_1"
    assert by["new-active"]["status"] != "suppressed"  # new activity since the route re-surfaces it
    assert by["handled"]["reason"] == "disposition: now, no new activity"
