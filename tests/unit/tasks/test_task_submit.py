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
