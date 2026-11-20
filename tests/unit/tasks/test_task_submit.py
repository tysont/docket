"""Submission validation (TD §9.4): coercions, partition, evidence, vocabulary, cross-field rules."""
from __future__ import annotations

import pytest

from docket.cli.errors import DocketError
from docket.config.task_models import TasksConfig
from docket.tasks import submit
from docket.tasks.kinds import registry
from tests.task_helpers import item, packs_for, persona_config, persona_kinds, raw_from_lines

pytestmark = pytest.mark.smoke
RERUN = "docket task submit --run r t_x /tmp/s.json"


def setup(kind_name="assess", persona="support-lead", n=3, tasks=None, **kw):
    cfg = persona_config(persona)
    kind = registry(TasksConfig.model_validate(tasks))[kind_name] if tasks else persona_kinds(persona)[kind_name]
    items = [item(i, source="tickets", **kw) for i in range(n)]
    pack = packs_for(kind, items, cfg)[0]
    return kind, pack


def kept(it, **over):
    row = {"id": it["id"], "source": it["source"], "keep": True, "score": 70, "kind": "request", "owner": "me",
           "ask": "Send the invented report", "evidence": [{"line": "L1", "quote": it["lines"][0]["text"]}]}
    row.update(over)
    return {k: v for k, v in row.items() if v is not None}


def run(kind, pack, rows):
    return submit.validate(kind, pack, {"items": rows}, raw_text=raw_from_lines(pack), rerun=RERUN)


def refusal(kind, pack, rows) -> DocketError:
    with pytest.raises(DocketError) as ei:
        run(kind, pack, rows)
    return ei.value


def test_declared_coercions():
    kind, pack = setup()
    a, b, c = pack["items"]
    rows = [kept(a, score="70", keep="true"),
            kept(b, evidence=b["lines"][0]["text"], deadline="2026-10-20T17:00:00Z"),
            kept(c, score=64.6, evidence=[c["lines"][0]["text"]], kind="Commitment")]
    out = run(kind, pack, rows).body["items"]
    assert out[0]["score"] == 70 and out[0]["keep"] is True
    assert out[1]["evidence"] == [{"quote": b["lines"][0]["text"]}] and out[1]["deadline"] == "2026-10-20"
    assert out[2]["score"] == 65 and out[2]["kind"] == "commitment"
    assert run(kind, pack, rows).shapes == []


def test_whitespace_differences_are_verbatim_but_paraphrase_is_not():
    kind, pack = setup(text="Please   send the invented\treport 1 by Friday.")
    a, b, c = pack["items"]
    ok = run(kind, pack, [kept(a, evidence=[{"line": "L1", "quote": " Please send the  invented report 1 by\nFriday. "}]),
                          kept(b), kept(c)])
    assert ok.body["items"][0]["evidence"][0]["quote"].startswith("Please send")
    e = refusal(kind, pack, [kept(a, evidence=[{"line": "L1", "quote": "Please send the report by Friday."}]),
                             kept(b), kept(c)])
    assert e.code == "task.rows_refused" and e.kind == "integrity"
    (row,) = e.details["rows"]
    assert row["code"] == "task.evidence_not_verbatim" and row["path"] == "items[0].evidence[0].quote"
    assert row["id"] == a["id"] and row["received"] == "Please send the report by Friday."
    assert row["fix"] and row["rule"] and e.details["rerun"] == RERUN and e.hint == RERUN


def test_unknown_and_denied_vocabulary_take_the_safe_reading_and_are_recorded():
    kind, pack = setup()
    a, b, c = pack["items"]
    got = run(kind, pack, [kept(a, kind="escalation"), kept(b, kind="other"), kept(c, ask="maybe check the queue")])
    rows = got.body["items"]
    assert rows[0]["kind"] == "fyi" and rows[1]["kind"] == "fyi"
    assert rows[2]["ask"] == "Follow up on check the queue"
    details = {(s.area, s.detail) for s in got.shapes}
    assert ("task.vocabulary", "assess.kind unknown value") in details
    assert ("task.vocabulary", "assess.kind denied value") in details
    assert ("task.vocabulary", "assess.ask denied leading word") in details
    assert all(s.extend for s in got.shapes)


def test_configured_vocabulary_extends_the_core_set():
    kind, pack = setup(tasks={"assess": {"kinds": {"escalation": "A customer escalation."},
                                         "ask": {"deny": ["investigate"], "default_verb": "Decide on"}}})
    a, b, c = pack["items"]
    got = run(kind, pack, [kept(a, kind="escalation"), kept(b, ask="Investigate the queue"), kept(c)])
    assert got.body["items"][0]["kind"] == "escalation"
    assert got.body["items"][1]["ask"] == "Decide on the queue"


def test_identity_partition_refuses():
    kind, pack = setup()
    a, b, c = pack["items"]
    for rows, key in (([kept(a), kept(b)], "missing"), ([kept(a), kept(b), kept(c), kept(c)], "duplicates"),
                      ([kept(a), kept(b), kept(c), {**kept(c), "id": "elsewhere"}], "extra")):
        e = refusal(kind, pack, rows)
        assert e.code == "integrity.partition" and e.details[key] and e.details["rerun"] == RERUN


def test_per_row_integrity_and_cross_field_errors_name_path_rule_and_fix():
    kind, pack = setup(n=4)
    a, b, c, d = pack["items"]
    e = refusal(kind, pack, [kept(a, source="mail"), kept(b, evidence=[{"line": "L9", "quote": b["lines"][0]["text"]}]),
                             kept(c, owner=None), {"id": d["id"], "source": "tickets", "keep": False, "score": 5}])
    rows = {r["code"]: r for r in e.details["rows"]}
    assert set(rows) == {"task.identity_mismatch", "task.evidence_unknown_line", "task.cross_field"}
    assert rows["task.identity_mismatch"]["path"] == "items[0].source" and "tickets" in rows["task.identity_mismatch"]["fix"]
    assert rows["task.evidence_unknown_line"]["fix"] == "use one of: L1, L2"
    assert rows["task.cross_field"]["path"] == "items[2].owner"
    assert "When `keep` is true" in rows["task.cross_field"]["rule"]
    assert e.details["refused"] == sorted([a["id"], b["id"], c["id"]]) and e.details["accepted_rows"] == 1


def test_schema_errors_are_input_errors_with_fixes():
    kind, pack = setup()
    a, b, c = pack["items"]
    e = refusal(kind, pack, [kept(a, score=150), kept(b, keep=None), kept(c, evidence=[{"line": "L1"}])])
    assert e.code == "task.invalid_result" and e.kind == "input"
    paths = {r["path"]: r for r in e.details["errors"]}
    assert paths["items[0].score"]["fix"] == "give `score` as: integer 0..100" and paths["items[0].score"]["received"] == 150
    assert paths["items[1].keep"]["fix"].startswith("add `keep`") and paths["items[1].keep"]["id"] == b["id"]
    assert "items[2].evidence[0].quote" in paths


def test_extra_fields_are_ignored_and_recorded():
    kind, pack = setup()
    got = run(kind, pack, [{**kept(it), "confidence": 0.9} for it in pack["items"]])
    assert "confidence" not in got.body["items"][0]
    assert ("task.extra_field", "assess.confidence") in {(s.area, s.detail) for s in got.shapes}


def test_review_rules_and_item_refs():
    cfg = persona_config("reference-like")
    kind = persona_kinds("reference-like")["review"]
    items = [item(i, keep=True, assessment={"kind": "request"}) for i in range(3)]
    pack = packs_for(kind, items, cfg)[0]
    ids = [it["id"] for it in pack["items"]]
    raw = raw_from_lines(pack)
    with pytest.raises(DocketError) as ei:
        submit.validate(kind, pack, {"items": [{"id": ids[0], "verdict": "ask"},
                                               {"id": ids[1], "verdict": "merge", "merge_with": "elsewhere"},
                                               {"id": ids[2], "verdict": "merge", "merge_with": ids[2]}]},
                        raw_text=raw, rerun=RERUN)
    codes = [(r["id"], r["code"]) for r in ei.value.details["rows"]]
    assert codes == [(ids[0], "task.cross_field"), (ids[1], "task.unknown_ref"), (ids[2], "task.unknown_ref")]
    got = submit.validate(kind, pack, {"items": [{"id": ids[0], "verdict": "ASK", "against": "Already handled."},
                                                 {"id": ids[1], "verdict": "merge", "merge_with": ids[0]},
                                                 {"id": ids[2], "verdict": "probably", "reason": "Unclear."}]},
                          raw_text=raw, rerun=RERUN)
    assert [r["verdict"] for r in got.body["items"]] == ["ask", "merge", "needs_verification"]


def test_workspace_kind_validates_like_a_core_kind():
    cfg = persona_config("support-lead")
    kind = persona_kinds("support-lead")["sla_check"]
    pack = packs_for(kind, [item(i, source="tickets") for i in range(2)], cfg)[0]
    a, b = pack["items"]
    raw = raw_from_lines(pack)
    with pytest.raises(DocketError) as ei:
        submit.validate(kind, pack, {"items": [{"id": a["id"], "breach_risk": "high"},
                                               {"id": b["id"], "breach_risk": "low", "related": "nowhere"}]},
                        raw_text=raw, rerun=RERUN)
    assert {(r["path"], r["code"]) for r in ei.value.details["rows"]} == {
        ("items[0].note", "task.cross_field"), ("items[0].time_left", "task.cross_field"),
        ("items[1].related", "task.unknown_ref")}
    got = submit.validate(kind, pack, {"items": [
        {"id": a["id"], "breach_risk": "High", "note": "Page the lead.", "time_left": "1h30m", "impact": "80"},
        {"id": b["id"], "breach_risk": "catastrophic", "related": a["id"]}]}, raw_text=raw, rerun=RERUN)
    rows = got.body["items"]
    assert rows[0] == {"id": a["id"], "breach_risk": "high", "time_left": 90, "impact": 80, "note": "Page the lead."}
    assert rows[1]["breach_risk"] == "medium"
    assert [s.extend for s in got.shapes] == ["tasks.kinds.sla_check.fields.breach_risk: values"]


DECISIONS = {"assess": {"decisions": {"act": "Do it now.", "park": "Put it on the task list.",
                                      "note": "Worth knowing; nothing is owed."}}}


def test_the_assess_task_recommends_a_decision_from_the_workspace_vocabulary():
    """A workspace names the decisions an item can get. With that vocabulary the assess task
    recommends one for every item it keeps; without it the output has no such field."""
    kind, pack = setup(tasks=DECISIONS)
    spec = {f.name: f for f in kind.fields}["decision"]
    assert spec.type == "enum" and set(spec.value_names()) == {"act", "park", "note"}
    it = pack["items"]
    out = run(kind, pack, [kept(it[0], decision="park"), kept(it[1], decision="Note"),
                           {"id": it[2]["id"], "source": it[2]["source"], "keep": False, "score": 5}])
    rows = {r["id"]: r for r in out.body["items"]}
    assert rows[it[0]["id"]]["decision"] == "park" and rows[it[1]["id"]]["decision"] == "note"
    assert "decision" not in rows[it[2]["id"]] or rows[it[2]["id"]]["decision"] is None
    e = refusal(kind, pack, [kept(it[0]), kept(it[1], decision="park"),
                             {"id": it[2]["id"], "source": it[2]["source"], "keep": False, "score": 5}])
    assert "decision" in str(e.details) and e.code.startswith("task.")       # a kept item needs one
    e = refusal(kind, pack, [kept(it[0], decision="escalate"), kept(it[1], decision="park"),
                             {"id": it[2]["id"], "source": it[2]["source"], "keep": False, "score": 5}])
    assert "escalate" in str(e.details) or "escalate" in e.message               # an unknown decision is refused, not guessed


def test_without_a_decision_vocabulary_the_assess_output_is_unchanged():
    kind, _ = setup()
    assert "decision" not in {f.name for f in kind.fields}


def test_a_reviewer_can_change_the_recommended_decision():
    """The reviewer sees the whole set with fresh eyes. Besides asking, merging or dropping,
    it may say an item deserves a different decision than the one first recommended."""
    cfg = persona_config("reference-like")
    kind = registry(TasksConfig.model_validate(DECISIONS))["review"]
    assert set({f.name: f for f in kind.fields}["decision"].value_names()) == {"act", "park", "note"}
    it = item(1, keep=True, assessment={"kind": "request", "owner": "me", "ask": "Reply", "decision": "act"})
    pack = packs_for(kind, [it], cfg)[0]
    assert pack["items"][0]["proposal"]["decision"] == "act"            # the reviewer sees what was recommended
    got = submit.validate(kind, pack, {"items": [{"id": "item001", "verdict": "ask", "against": "It may be settled.",
                                                  "decision": "note"}]}, raw_text=raw_from_lines(pack), rerun=RERUN)
    assert got.body["items"][0]["decision"] == "note"
    plain = persona_kinds("reference-like")["review"]
    assert "decision" not in {f.name for f in plain.fields}                # no vocabulary: no such field


def test_the_reviewers_decision_replaces_the_first_recommendation():
    from docket.tasks.runner import NodePlan, combine_review
    items = [{"id": "a", "keep": True, "assessment": {"decision": "act", "ask": "Reply"}, "trace": []},
             {"id": "b", "keep": True, "assessment": {"decision": "act", "ask": "Reply"}, "trace": []}]
    rows = [{"id": "a", "verdict": "ask", "against": "Settled already.", "decision": "note"},
            {"id": "b", "verdict": "ask", "against": "Could wait."}]
    plan = NodePlan(node="review", kind=persona_kinds("reference-like")["review"], packs=[{"task_id": "t1"}],
                    results={"t1": {"items": rows}}, issued={"t1"})
    out = {it["id"]: it for it in combine_review(items, plan)["items"]}
    assert out["a"]["assessment"]["decision"] == "note" and out["a"]["review"]["decision"] == "note"
    last = out["a"]["trace"][-1]
    assert last["decision"] == "note" and last["was"] == "act"
    assert (last["verb"], last["node"], last["task"]) == ("judge", "review", "t1")
    assert out["b"]["assessment"]["decision"] == "act" and "decision" not in out["b"]["review"]


def test_a_review_row_may_give_a_deadline_and_an_estimate_read_like_the_assessments():
    """A reviewer who turns an item into work may say when it is due and how long it takes.
    The two fields are typed and read as the assess fields are: a date, and a duration in
    minutes. A value that is not one is read as none and recorded, never refused. A merge or
    drop row may carry them too; nothing refuses it."""
    cfg = persona_config("reference-like")
    kind = registry(TasksConfig.model_validate(DECISIONS))["review"]
    assess = registry(TasksConfig.model_validate(DECISIONS))["assess"]
    for name in ("deadline", "est"):
        assert kind.field(name).type == assess.field(name).type and not kind.field(name).required
    assert "deadline" in {f.name for f in persona_kinds("reference-like")["review"].fields}  # no vocabulary needed
    items = [item(i, keep=True, assessment={"kind": "request", "owner": "me", "ask": "Reply", "decision": "note"})
             for i in range(4)]
    pack = packs_for(kind, items, cfg)[0]
    a, b, c, d = (it["id"] for it in pack["items"])
    got = submit.validate(kind, pack, {"items": [
        {"id": a, "verdict": "ask", "against": "It may wait.", "decision": "park",
         "deadline": "2026-10-16T17:00:00Z", "est": "1h"},
        {"id": b, "verdict": "needs_verification", "reason": "Unclear.", "deadline": "before the meeting",
         "est": "a while"},
        {"id": c, "verdict": "merge", "merge_with": a, "deadline": "2026-10-20", "est": 30},
        {"id": d, "verdict": "drop", "reason": "Handled.", "deadline": "2026-10-21"}]},
        raw_text=raw_from_lines(pack), rerun=RERUN)
    rows = {r["id"]: r for r in got.body["items"]}
    assert (rows[a]["deadline"], rows[a]["est"]) == ("2026-10-16", 60)
    assert rows[b].get("deadline") is None and rows[b].get("est") is None
    assert (rows[c]["deadline"], rows[c]["est"]) == ("2026-10-20", 30) and rows[d]["deadline"] == "2026-10-21"
    assert {s.detail for s in got.shapes} == {"review.deadline not a date", "review.est not a duration"}


def _review_plan(rows):
    from docket.tasks.runner import NodePlan
    return NodePlan(node="review", kind=persona_kinds("reference-like")["review"], packs=[{"task_id": "t1"}],
                    results={"t1": {"items": rows}}, issued={"t1"})


def test_the_reviewers_deadline_and_estimate_replace_the_assessments_and_the_trace_keeps_both():
    """The assessment thought an item needed nothing, so it gave no deadline. The reviewer makes
    it work and names the day it is needed: that day and the estimate stand on the item, as the
    reviewer's decision does, and the trace keeps what the assessment said. Fields the reviewer
    left out keep the assessment's values. On a drop or a merge row they are applied the same
    way, whatever the verdict; the item leaves the kept set, so they change nothing asked."""
    from docket.tasks.runner import combine_review
    base = {"keep": True, "trace": []}
    items = [{"id": "a", **base, "assessment": {"decision": "note", "ask": "Reply", "est": 15}},
             {"id": "b", **base, "assessment": {"decision": "act", "ask": "Reply", "deadline": "2026-10-30",
                                                "est": 15}},
             {"id": "c", **base, "assessment": {"decision": "act", "ask": "Reply", "deadline": "2026-10-30"}},
             {"id": "d", **base, "assessment": {"decision": "act", "ask": "Reply"}}]
    rows = [{"id": "a", "verdict": "ask", "against": "It may wait.", "decision": "act", "deadline": "2026-10-16",
             "est": 45, "reason": "Needed for tomorrow's meeting."},
            {"id": "b", "verdict": "needs_verification", "reason": "Check the date.", "deadline": "2026-10-20"},
            {"id": "c", "verdict": "ask", "against": "Could wait."},
            {"id": "d", "verdict": "drop", "reason": "Handled.", "deadline": "2026-10-21", "est": 30}]
    out = {it["id"]: it for it in combine_review(items, _review_plan(rows))["items"]}
    a = out["a"]
    assert a["assessment"] == {"decision": "act", "ask": "Reply", "est": 45, "deadline": "2026-10-16"}
    assert (a["review"]["deadline"], a["review"]["est"]) == ("2026-10-16", 45)
    last = a["trace"][-1]
    assert (last["was"], last["deadline"], last["was_deadline"], last["est"], last["was_est"]) == \
        ("note", "2026-10-16", None, 45, 15)
    b = out["b"]
    assert (b["assessment"]["deadline"], b["assessment"]["est"]) == ("2026-10-20", 15)
    assert b["trace"][-1]["was_deadline"] == "2026-10-30" and "was_est" not in b["trace"][-1]
    assert "was" not in b["trace"][-1]                       # the decision was not touched
    c = out["c"]
    assert c["assessment"]["deadline"] == "2026-10-30" and "deadline" not in c["review"]
    assert not {"was_deadline", "was_est"} & set(c["trace"][-1])
    d = out["d"]
    assert d["keep"] is False and d["assessment"]["deadline"] == "2026-10-21"
    assert d["trace"][-1]["was_deadline"] is None


def test_a_reviewer_can_clear_a_deadline_but_not_also_give_one():
    """The assessment may name a due date the candidate's lines never give. The reviewer clears
    it with ``no_deadline``. A row that clears the deadline and gives one says two opposite
    things, so it is refused with the row, the rule and the fix."""
    cfg = persona_config("reference-like")
    kind = persona_kinds("reference-like")["review"]
    spec = kind.field("no_deadline")
    assert spec is not None and spec.type == "bool" and not spec.required
    items = [item(i, keep=True, assessment={"kind": "request", "owner": "me", "ask": "Reply",
                                            "deadline": "2026-10-20"}) for i in range(2)]
    pack = packs_for(kind, items, cfg)[0]
    a, b = (it["id"] for it in pack["items"])
    got = submit.validate(kind, pack, {"items": [
        {"id": a, "verdict": "ask", "against": "It may wait.", "no_deadline": True},
        {"id": b, "verdict": "needs_verification", "reason": "Unclear.", "no_deadline": "true"}]},
        raw_text=raw_from_lines(pack), rerun=RERUN)
    rows = {r["id"]: r for r in got.body["items"]}
    assert rows[a]["no_deadline"] is True and rows[b]["no_deadline"] is True
    with pytest.raises(DocketError) as ei:
        submit.validate(kind, pack, {"items": [
            {"id": a, "verdict": "ask", "against": "It may wait.", "no_deadline": True, "deadline": "2026-10-16"},
            {"id": b, "verdict": "ask", "against": "It may wait."}]}, raw_text=raw_from_lines(pack), rerun=RERUN)
    e = ei.value
    assert e.code == "task.rows_refused"
    (row,) = e.details["rows"]
    assert (row["id"], row["path"], row["code"]) == (a, "items[0].no_deadline", "task.cross_field")
    assert "`deadline`" in row["rule"] and "`no_deadline`" in row["rule"] and row["fix"]


def test_a_cleared_deadline_leaves_the_item_and_the_trace_keeps_it():
    """A cleared deadline is taken off the assessment, so nothing downstream reads it. The trace
    keeps the old date under ``was_deadline``, as it does for a replaced one. Clearing a date the
    item never had changes nothing but the trace."""
    from docket.tasks.runner import combine_review
    base = {"keep": True, "trace": []}
    items = [{"id": "a", **base, "assessment": {"decision": "act", "ask": "Reply", "deadline": "2026-10-20",
                                                "est": 30}},
             {"id": "b", **base, "assessment": {"ask": "Reply"}}]
    rows = [{"id": "a", "verdict": "ask", "against": "Could wait.", "no_deadline": True,
             "reason": "The lines name no date."},
            {"id": "b", "verdict": "ask", "against": "Could wait.", "no_deadline": True}]
    out = {it["id"]: it for it in combine_review(items, _review_plan(rows))["items"]}
    a = out["a"]
    assert a["assessment"] == {"decision": "act", "ask": "Reply", "est": 30}
    assert a["review"]["no_deadline"] is True and a["keep"] is True
    last = a["trace"][-1]
    assert last["was_deadline"] == "2026-10-20" and last["no_deadline"] is True and "deadline" not in last
    b = out["b"]
    assert b["assessment"] == {"ask": "Reply"} and b["trace"][-1]["was_deadline"] is None


def test_a_workspace_can_ask_the_assessment_for_extra_words_about_an_item():
    """Some destinations want an item said another way than its ask: as a topic for a meeting,
    say. A workspace names such a field and says what it should be; the assessment may fill
    it, and it reaches every template under ``extra``."""
    from types import SimpleNamespace
    from docket.config import contexts
    from docket.decide import options
    cfg = TasksConfig.model_validate({"assess": {"extra": {"agenda": "The item as a topic for a meeting."}}})
    kind = registry(cfg)["assess"]
    spec = {f.name: f for f in kind.fields}["agenda"]
    assert not spec.required and spec.type == "str" and "topic for a meeting" in spec.description
    assert "agenda" not in {f.name for f in persona_kinds("reference-like")["assess"].fields}
    assert "extra.*" in contexts._ITEM
    rc = SimpleNamespace(ws=SimpleNamespace(config=SimpleNamespace(
        sources={}, profile=None, tasks=cfg)))
    real = options.area_for, options.forum_for
    options.area_for, options.forum_for = (lambda _rc, _it: ""), (lambda _rc, _it: None)
    try:
        view = options.item_view(rc, {"id": "a", "source": "s",
                                      "assessment": {"ask": "Reply", "agenda": "Capacity plan: who signs off?"}})
        bare = options.item_view(rc, {"id": "b", "source": "s", "assessment": {"ask": "Reply"}})
    finally:
        options.area_for, options.forum_for = real
    assert view["extra"] == {"agenda": "Capacity plan: who signs off?"} and bare["extra"] == {"agenda": ""}
    with pytest.raises(Exception):
        TasksConfig.model_validate({"assess": {"extra": {"ask": "collides with a core field"}}})
