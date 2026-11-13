"""What holds a writing node (TD §13.3, FR-APP-07): every operation whose outcome is not
``applied`` or ``already_present``, until it is repeated or accepted as unwritten. All data is
invented."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from docket.engine.nodes import held_rows, incomplete, unwritten_digest

pytestmark = pytest.mark.smoke

OPS = [{"op_id": "op_a", "verb": "create", "title": "Draft the memo"},
       {"op_id": "op_b", "verb": "append", "line": "- [ ] Reply to the venue"},
       {"op_id": "op_c", "verb": "update", "title": "Plan the week"},
       {"op_id": "op_d", "verb": "append", "line": "- [ ] Depends on b"}]
ROWS = [{"op_id": "op_a", "destination": "cal", "outcome": "refused",
         "error": {"code": "dest.state_changed", "message": "an owned event already exists", "retryable": False}},
        {"op_id": "op_b", "destination": "tasks", "outcome": "failed",
         "error": {"code": "dest.apply_failed", "message": "disk full", "retryable": True}},
        {"op_id": "op_c", "destination": "cal", "outcome": "already_present"},
        {"op_id": "op_d", "destination": "tasks", "outcome": "blocked", "blocked_by": ["op_b"]}]


def run(unwritten=None):
    return SimpleNamespace(run=SimpleNamespace(header=SimpleNamespace(unwritten=unwritten or {})))


def test_refused_failed_and_blocked_operations_are_incomplete_with_their_errors():
    rows = incomplete(ROWS, OPS)
    assert [(r["op_id"], r["outcome"], r["code"], r["retryable"]) for r in rows] == [
        ("op_a", "refused", "dest.state_changed", False),
        ("op_b", "failed", "dest.apply_failed", True),
        ("op_d", "blocked", None, True)]
    assert [r["what"] for r in rows] == ["Draft the memo", "- [ ] Reply to the venue", "- [ ] Depends on b"]
    assert rows[2]["message"] == "waits on op_b"


def test_a_node_holds_until_exactly_its_incomplete_operations_are_accepted():
    data = {"receipts": ROWS, "incomplete": incomplete(ROWS, OPS)}
    assert len(held_rows(run(), "apply", data)) == 3
    ok = {"apply": {"digest": unwritten_digest(data["incomplete"])}}
    assert held_rows(run(ok), "apply", data) == []
    # a later outcome that differs from what was accepted holds the node again
    other = incomplete(ROWS[:2], OPS)
    assert len(held_rows(run(ok), "apply", {"receipts": ROWS[:2], "incomplete": other})) == 2
    # an artifact written before ``incomplete`` existed is read from its receipts
    assert [r["op_id"] for r in held_rows(run(), "apply", {"receipts": ROWS})] == ["op_a", "op_b", "op_d"]
    assert held_rows(run(), "apply", {"receipts": [ROWS[2]], "incomplete": []}) == []
