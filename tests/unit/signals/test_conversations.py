"""Conversations (TD §7.2): a unit that has no threads, such as a direct or group
conversation, still holds exchanges. ``signals.conversations`` names such units and the
quiet gap that separates one exchange from the next; the items of one exchange share a
conversation key, merge into one topic, and reach a task together.
"""
from __future__ import annotations

from docket.signals.conversations import assign
from docket.signals.engine import SignalEnv, evaluate, instant
from docket.signals.merge import MergeInputs, build_merge
from docket.signals.models import parse
from docket.config.models import Profile
from datetime import date
from tests.signal_helpers import NOW, PROFILE, item

UNITS = {("dms", "d1"): {"name": "d1", "kind": "direct", "class": "dm", "tier": "1"},
         ("dms", "d2"): {"name": "d2", "kind": "direct", "class": "dm", "tier": "1"},
         ("chat", "room"): {"name": "Ops", "kind": "room", "class": "service", "tier": "2"}}
CONV = [{"unit": {"kind": "direct"}, "gap": "2h"}]


def env(conversations=CONV):
    cfg = parse({"rules": [], "conversations": conversations})
    return SignalEnv(cfg, Profile.model_validate(PROFILE), [], UNITS, {"chat": "codemode", "dms": "codemode"},
                     instant(NOW), date(2026, 10, 15))


def dm(i, at, unit="d1", **kw):
    # A system without threads gives every message a thread of its own.
    return item(i, source="dms", unit=unit, thread=f"{unit}/threads/{i}", at=f"2026-10-15T{at}:00+00:00",
                text=kw.pop("text", f"message {i}"), **kw)


def test_messages_closer_than_the_gap_are_one_conversation():
    items = [dm("a", "09:00"), dm("b", "09:40"), dm("c", "11:30"),      # one exchange: each within 2h of the last
             dm("d", "15:00"), dm("e", "15:05"),                         # a second, after a quiet afternoon
             dm("x", "09:10", unit="d2")]                                # another unit is another conversation
    out = {it["id"]: it for it in assign([dict(i) for i in reversed(items)], env().cfg, UNITS)}
    assert out["a"]["conversation"] == out["b"]["conversation"] == out["c"]["conversation"] == "dms:d1:a"
    assert out["d"]["conversation"] == out["e"]["conversation"] == "dms:d1:d"
    assert out["x"]["conversation"] == "dms:d2:x"


def test_a_unit_no_rule_names_keeps_its_own_threads():
    items = [item("r1", source="chat", unit="room", thread="room/threads/t1"),
             item("r2", source="chat", unit="room", thread="room/threads/t2")]
    out = assign([dict(i) for i in items], env().cfg, UNITS)
    assert all("conversation" not in it or it["conversation"] is None for it in out)
    assert all(it.get("conversation") is None for it in assign([dict(i) for i in [dm("a", "09:00")]], env([]).cfg, UNITS))


def test_a_conversation_merges_into_one_topic_with_its_strongest_message_in_front():
    items = [dm("a", "09:00", text="thoughts on the renewal?"), dm("b", "09:20", text="we need a decision by friday"),
             dm("c", "09:30", text="agreed"), dm("z", "16:00", text="separate thing")]
    agent = {"a": {"keep": True, "score": 60}, "b": {"keep": True, "score": 90}, "c": {"keep": False, "score": 5},
             "z": {"keep": True, "score": 70}}
    data = build_merge(MergeInputs(items=items, env=env(), agent=agent))
    by = {it["id"]: it for it in data["items"]}
    assert set(by) == {"b", "z"}                                    # one finding per exchange, not per message
    assert sorted(m["id"] for m in by["b"]["members"]) == ["a", "c"]
    assert by["b"]["topic_basis"] == "conversation" and by["b"]["topic"] == "conv:dms:d1:a"


def test_my_reply_later_in_the_conversation_answers_it():
    items = [dm("a", "09:00", text="can you review this?"), dm("b", "09:10", text="done", handle="u/100", **{"from": "Rae Holm"})]
    data = build_merge(MergeInputs(items=items, env=env(), agent={"a": {"keep": True, "score": 90}}))
    (it,) = data["items"]
    assert it["status"] == "suppressed" and it["reason"].startswith("answered")


def test_evaluated_items_carry_the_conversation_so_a_task_pack_can_keep_it_together():
    out = {it["id"]: it for it in evaluate([dm("a", "09:00"), dm("b", "09:20")], env())}
    assert out["a"]["conversation"] == out["b"]["conversation"] == "dms:d1:a"
    assert "conv:dms:d1:a" in out["b"]["keys"]


def test_the_gap_must_be_a_duration():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        parse({"conversations": [{"unit": {"kind": "direct"}, "gap": "a while"}]})
    assert parse({"conversations": [{"unit": {"kind": "direct"}}]}).conversations[0].gap == "4h"
