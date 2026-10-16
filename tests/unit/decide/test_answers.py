"""Answers (TD §10.3, FR-DEC-04, FR-DEC-06): override normalization, binding, merges and undo."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from docket.cli.errors import DocketError
from docket.decide.answers import (
    bind, check_required, command_args, normalize_overrides, resolve_merge_targets, with_answer, without_answer,
)
from docket.decide.options import guard_ok, resolve_option

pytestmark = pytest.mark.smoke
REF = date(2026, 10, 15)  # a Thursday


def fake_rc():
    forums = [SimpleNamespace(id="platform-sync", name="Platform Sync"), SimpleNamespace(id="ops-review", name="Ops Review")]
    topics = [SimpleNamespace(id="ingest", area="Platform")]
    return SimpleNamespace(ws=SimpleNamespace(config=SimpleNamespace(profile=SimpleNamespace(forums=forums,
                                                                                             topics=topics))))


def test_every_override_is_normalized():
    ov = normalize_overrides(fake_rc(), {
        "due": "Monday", "est": "60 minutes", "when": "thu 13:00-17:00", "title": "  Draft   the memo ",
        "action": "Write the memo", "links": ["https://t.example/1", "https://t.example/1"], "link": "https://t.example/2",
        "area": "platform", "forum": "platform sync", "merge_with": "q_b", "note": "after the review"}, REF)
    assert ov == {"due": "2026-10-19", "est": 60, "when": "2026-10-22 13:00-17:00", "title": "Draft the memo",
                  "action": "Write the memo", "links": ["https://t.example/1", "https://t.example/2"],
                  "area": "Platform", "forum": "platform-sync", "merge_with": ["q_b"], "note": "after the review"}
    assert command_args(ov) == [
        "--due", "2026-10-19", "--est", "1h", "--when", "2026-10-22 13:00-17:00", "--title", "Draft the memo",
        "--action", "Write the memo", "--link", "https://t.example/1", "--link", "https://t.example/2",
        "--area", "Platform", "--forum", "platform-sync", "--merge-with", "q_b", "--note", "after the review"]


@pytest.mark.parametrize("raw,expected", [
    ({"est": "1.5h"}, {"est": 90}), ({"est": 45}, {"est": 45}), ({"est": "1h30m"}, {"est": 90}),
    ({"due": "tomorrow"}, {"due": "2026-10-16"}), ({"due": "thu"}, {"due": "2026-10-22"}),
    ({"when": "Offsite trip"}, {"when": "event:Offsite trip"}), ({"when": "this week"}, {"when": "2026-10-15..2026-10-18"}),
    ({"forum": "ops"}, {"forum": "ops-review"}), ({"area": "Billing"}, {"area": "Billing"}),
])
def test_override_forms(raw, expected):
    got = normalize_overrides(fake_rc(), raw, REF)
    assert {k: got[k] for k in expected} == expected


@pytest.mark.parametrize("raw,code", [
    ({"due": "someday"}, "input.bad_date"), ({"est": "lots"}, "input.bad_duration"),
    ({"when": "thu 17:00-13:00"}, "input.bad_window"), ({"forum": "nowhere"}, "input.unknown_forum"),
    ({"bogus": 1}, "input.bad_argument"), ({"title": "   "}, "input.bad_argument"),
])
def test_bad_overrides_refuse(raw, code):
    with pytest.raises(DocketError) as ei:
        normalize_overrides(fake_rc(), raw, REF)
    assert ei.value.code == code


def Q(qid, subject="s", options=("task", "dismiss"), kind="finding"):
    return {"id": qid, "kind": kind, "subject": subject, "options": [{"id": o} for o in options]}


def test_bind_keeps_unchanged_answers_and_moves_changed_ones_to_previous():
    qs = [Q("q_a", "s1"), Q("q_b", "s2-new"), Q("q_c", "s3", options=("dismiss",)), Q("q_d", "s4")]
    stored = {"q_a": {"option": "task", "subject": "s1"}, "q_b": {"option": "task", "subject": "s2"},
              "q_c": {"option": "task", "subject": "s3"}, "q_d": {"option": "dismiss"}}  # no subject: older answers bind
    bound, previous = bind(qs, stored)
    assert set(bound) == {"q_a", "q_d"} and set(previous) == {"q_b", "q_c"}


def test_merge_binds_with_its_host_and_undo_frees_it():
    qs = {q["id"]: q for q in [Q("q_a"), Q("q_b"), Q("q_c")]}
    host = {"kind": "finding", "option": "task", "overrides": {"merge_with": ["q_b"]}, "subject": "s"}
    after = with_answer({}, qs["q_a"], host, qs, {})
    assert after["q_b"] == {"kind": "finding", "option": "merged", "action": "merged", "merged_into": "q_a",
                            "overrides": {}, "subject": "s"}
    bound, _ = bind(list(qs.values()), after)
    assert set(bound) == {"q_a", "q_b"}
    # re-answering the host without the merge frees the absorbed question
    freed = with_answer(after, qs["q_a"], {"option": "task", "overrides": {}}, qs, {})
    assert "q_b" not in freed
    # undoing the host removes both; undoing the absorbed one edits the host
    gone, removed = without_answer(after, "q_a")
    assert gone == {} and removed == ["q_a", "q_b"]
    edited, removed = without_answer(after, "q_b")
    assert removed == ["q_b"] and "merge_with" not in edited["q_a"]["overrides"]
    # a host whose own answer was unbound records it as previous
    re = with_answer({"q_a": {"option": "dismiss", "subject": "old"}}, qs["q_a"], {"option": "task"}, qs,
                     {"q_a": {"option": "dismiss"}})
    assert re["q_a"]["previous"] == {"option": "dismiss", "subject": "old"}


def test_merge_targets_are_checked():
    qs = {q["id"]: q for q in [Q("q_aa1"), Q("q_bb2"), Q("r_x", kind="reconcile")]}
    assert resolve_merge_targets(["q_bb"], "q_aa1", qs, {}) == ["q_bb2"]
    for target, answers in (("q_aa1", {}), ("q_zz", {}), ("r_x", {}),
                            ("q_bb2", {"q_bb2": {"overrides": {"merge_with": ["q_cc"]}}})):
        with pytest.raises(DocketError) as ei:
            resolve_merge_targets([target], "q_aa1", qs, answers)
        assert ei.value.code == "input.bad_merge"


def test_required_overrides_name_the_fix():
    q = {"id": "r_k", "requires": {"partly": ["est"], "rescope": ["est|title|action|due"]}}
    check_required(q, "partly", {"est": 30}, ["docket"])
    check_required(q, "rescope", {"title": "Smaller"}, ["docket"])
    with pytest.raises(DocketError) as ei:
        check_required(q, "partly", {}, ["docket", "ask", "answer", "r_k", "partly"])
    assert ei.value.code == "input.missing_override" and ei.value.hint.endswith("partly --est <est>")


OPTS = [
    {"id": "task", "label": "Add a task (due Mon)", "action": "task.add", "destination": "backlog"},
    {"id": "agenda", "label": "Raise it at Platform Sync", "action": "agenda.add", "destination": "agendas"},
    {"id": "block", "label": "Block 1h this week", "action": "calendar.place"},
    {"id": "watch", "label": "Watch for new activity", "action": "watch"},
    {"id": "dismiss", "label": "Dismiss", "action": "dismiss"},
]


@pytest.mark.parametrize("text,expected", [
    ("task", "task"), ("Task", "task"), ("3", "block"), ("dismiss", "dismiss"), ("raise it", "agenda"),
    ("RAISE IT AT PLATFORM SYNC", "agenda"), ("backlog", "task"), ("agendas", "agenda"),
    ("calendar.place", "block"), ("watch for", "watch"), ("bl", "block"), ("add a", "task"),
])
def test_forgiving_resolution(text, expected):
    assert resolve_option(text, OPTS)["id"] == expected


def test_ambiguous_destination_names_both_candidates():
    opts = OPTS + [{"id": "escalate", "label": "Escalate", "action": "task.add", "destination": "backlog"}]
    with pytest.raises(DocketError) as ei:
        resolve_option("backlog", opts, question_id="q_1")
    assert ei.value.code == "input.ambiguous_option"
    assert {c["id"] for c in ei.value.details["candidates"]} == {"task", "escalate"}


def test_guards():
    it = {"source": "tickets", "sender": {"handle": "pat@customer.example", "relation": "external",
                                          "relations": ["external"], "is_me": False}}
    with_forum = {"forum": "platform-sync", "due_source": "deadline"}
    none = {"forum": None, "due_source": "lead_days"}
    assert guard_ok(None, "has_forum", it, with_forum) and not guard_ok(None, "has_forum", it, none)
    assert guard_ok(None, "has_deadline", it, with_forum) and not guard_ok(None, "has_deadline", it, none)
    assert guard_ok(None, "has_reply_channel", it, none)
    assert not guard_ok(None, "has_reply_channel", {**it, "sender": {"handle": "me", "is_me": True}}, none)
    assert guard_ok(None, "source_is:pager,tickets", it, none) and not guard_ok(None, "source_is:pager", it, none)
    assert guard_ok(None, "relation_is:external", it, none) and not guard_ok(None, "relation_is:reports", it, none)
    assert guard_ok(None, None, it, none)
