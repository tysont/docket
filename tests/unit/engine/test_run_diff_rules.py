"""``run diff`` comparison rules (TD §5.10): matching by topic, significance order, bounds."""
from __future__ import annotations

from typing import Any

import pytest

from docket.engine.run_diff import _op_text, _reason, compare, text

pytestmark = pytest.mark.smoke


def snap(items: dict[str, dict[str, Any]] | None = None, questions: dict[str, dict[str, Any]] | None = None,
         answers: dict[str, dict[str, Any]] | None = None, ops: dict[tuple[str, str, str], dict[str, Any]] | None = None,
         read: int = 0) -> dict[str, Any]:
    items, questions, answers, ops = items or {}, questions or {}, answers or {}, ops or {}
    return {"counts": {"read": read, "kept": sum(1 for i in items.values() if i["status"] == "kept"),
                       "asked": len(questions), "answered": len(answers), "operations": len(ops)},
            "items": items, "questions": questions, "answers": answers, "ops": ops}


def it(topic: str, status: str = "kept", score: int = 50, reason: str | None = None, **kw: Any) -> dict[str, Any]:
    return {"id": topic, "topic": topic, "title": f"Item {topic}", "status": status, "score": score,
            "reason": reason, **kw}


def op(dest: str, what: str, outcome: str | None = None) -> tuple[tuple[str, str, str], dict[str, Any]]:
    return (dest, "append", what), {"destination": dest, "verb": "append", "text": f"add {what}", "outcome": outcome}


def test_items_are_matched_by_topic_and_kept_changes_come_highest_score_first() -> None:
    a = snap({"t1": it("t1", score=30), "t2": it("t2", "dropped", 10, "below the floor"), "t3": it("t3", score=80)})
    b = snap({"t1": it("t1", "dropped", 25, "below the floor"), "t2": it("t2", score=60), "t4": it("t4", score=90)})
    d = compare(a, b)
    assert [(r["topic"], r["kept_in"]) for r in d["kept"]["rows"]] == [("t4", "b"), ("t3", "a"), ("t2", "b"),
                                                                        ("t1", "a")]
    t3 = d["kept"]["rows"][1]
    assert t3["b"]["reason"] == "not read" and d["kept"]["rows"][3]["b"]["reason"] == "dropped: below the floor"
    assert d["changed"]["total"] == 0  # a score that moved an item in or out is shown with the kept change
    assert d["counts"]["read"] == {"a": 0, "b": 0, "change": 0}


def test_answers_come_before_recommendations_and_then_the_largest_score_change() -> None:
    items_a = {t: it(t, score=50) for t in ("t1", "t2", "t3", "t4")}
    items_b = {"t1": it("t1", score=52), "t2": it("t2", score=90), "t3": it("t3", score=50), "t4": it("t4", score=50)}
    qa = {t: {"id": f"q_{t}", "recommended": "task"} for t in ("t3", "t4")}
    qb = {"t3": {"id": "q_t3", "recommended": "task"}, "t4": {"id": "q_t4", "recommended": "dismiss"}}
    d = compare(snap(items_a, qa, {"t3": {"option": "task"}}),
                snap(items_b, qb, {"t3": {"option": "task", "overrides": {"est": 45}}}))
    rows = d["changed"]["rows"]
    assert [r["topic"] for r in rows] == ["t3", "t4", "t2", "t1"]
    assert rows[0]["answer"] == {"a": "task", "b": "task (est=45)"}
    assert rows[1]["recommended"] == {"a": "task", "b": "dismiss"}
    assert rows[2]["score"] == {"a": 50, "b": 90}


def test_a_question_without_an_item_is_matched_by_its_id() -> None:
    qa = {"r_block": {"id": "r_block", "prompt": "Did the block happen?"}}
    d = compare(snap(questions=qa, answers={"r_block": {"option": "done"}}),
                snap(questions=qa, answers={"r_block": {"option": "moved"}}))
    assert d["changed"]["rows"] == [{"topic": "r_block", "title": "Did the block happen?",
                                     "answer": {"a": "done", "b": "moved"}}]


def test_operations_differ_by_destination_most_differences_first_and_groups_are_bounded() -> None:
    a = snap(ops=dict([op("tasks", "x", "applied"), op("tasks", "y"), op("cal", "z")]))
    b = snap(ops=dict([op("tasks", "x", "failed"), op("tasks", "w1"), op("tasks", "w2"), op("tasks", "w3")]))
    d = compare(a, b, limit=2)
    dests = d["operations"]["rows"]
    assert [x["destination"] for x in dests] == ["tasks", "cal"]
    tasks = dests[0]
    assert (tasks["a"], tasks["b"]) == (2, 4)
    assert tasks["only_b"] == {"total": 3, "rows": ["add w1", "add w2"]}
    assert tasks["only_a"]["rows"] == ["add y"] and tasks["outcome"]["rows"][0]["b"] == "failed"
    out = text({"a": {"run": "r1", "graph": "g", "status": "sealed", "sandbox": False},
                "b": {"run": "r2", "graph": "g", "status": "open", "sandbox": True}}, d)
    assert "  tasks: 2 -> 4" in out and "    ... 1 more only in B" in out and "    applied -> failed: add x" in out
    assert out.splitlines()[:2] == ["A r1 (sealed)", "B r2 (open, sandbox)"]
    assert "kept in one run only: none" in out


def test_the_reason_comes_from_the_trace_when_the_item_names_none() -> None:
    assert _reason(it("t", "dropped", 20, trace=[{"step": "rule", "rule": "noise"}])) == "dropped: rule noise"
    assert _reason(it("t", "kept", 70, trace=[{"step": "kept", "score": 70}])) == "kept at score 70"
    assert _reason(None) == "not read"


def test_an_operation_reads_as_what_it_does_then_where() -> None:
    o = {"destination": "backlog"}
    assert _op_text("- backlog (tasks.md under ## Inbox): add `- [ ] x`", o) == "add `- [ ] x` (tasks.md under ## Inbox)"
    assert _op_text("- backlog: draft `hello` to a@example.org", o) == "draft `hello` to a@example.org"
