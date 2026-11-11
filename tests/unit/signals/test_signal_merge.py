"""Mapping, identity, merge, suppression and coverage (TD §7, §8.3, FR-SIG-01, 07, 08)."""
from __future__ import annotations

from docket.items.mapping import item_id, map_record
from docket.items.model import Item
from docket.shapes.unexpected import collecting
from docket.signals.merge import MergeInputs, build_merge
from tests.signal_helpers import env, item

REFS = {"incident": "\\bINC-\\d+\\b"}


def merge(items, rules=(), **kw):
    e = env(list(rules), references=kw.pop("references", REFS), scoring=kw.pop("scoring", None))
    data = build_merge(MergeInputs(items=items, env=e, **kw))
    return {it["id"]: it for it in data["items"]}, data


# --- mapping and identity -------------------------------------------------------------


def test_mapping_structure_keys_and_identity():
    rec = {"name": "m1", "title": "Hello", "text": "hello", "sender": {"name": "u/300", "displayName": "Tam Ober"},
           "thread": {"name": "t/1"}, "to": [{"email": "rae@orbit.example"}, "x@orbit.example"], "cc": "c@o.example"}
    mp = {"id": "$.name", "text": "$.text", "from": "$.sender.displayName", "from.handle": "$.sender.name",
          "thread": "$.thread.name", "to": "$.to", "cc": "$.cc", "mentions": "$.annotations"}
    with collecting() as col:
        it = map_record("chat", rec, mp, fallback_identity="blob#0")
    assert it["from"] == "Tam Ober" and it["handle"] == "u/300" and it["thread"] == "t/1"
    assert it["to"] == ["rae@orbit.example", "x@orbit.example"] and it["cc"] == ["c@o.example"]
    assert it["id"] == item_id("chat", "", "m1") and len(it["id"]) == 16
    assert col.drain() == []  # mentions is mapped but absent: structure keys are not shapes
    bare = map_record("mail", {"id": "x", "from": "eve@client-co.example"}, {"from": "$.from"},
                      fallback_identity="b#1")
    assert bare["handle"] == "eve@client-co.example" and bare["thread"] == "" and bare["to"] == []
    assert map_record("mail", {"id": "x"}, {}, fallback_identity="b#1")["id"] == bare["id"]  # stable identity


def test_merge_artifact_items_validate_against_the_item_model():
    by, _ = merge([item("a", text="hi")])
    Item.model_validate(by["a"])
    assert set(by["a"]) >= {"id", "topic", "sender", "signals", "score", "components", "status", "trace", "from"}


# --- merge ------------------------------------------------------------------------------


def test_merge_across_sources_by_reference_keeps_the_strongest():
    items = [item("chat1", text="pause the migration for INC-2041?", handle="u/300"),
             item("mail1", source="mail", title="INC-2041 follow-up", handle="vic@orbit.example"),
             item("other", text="unrelated")]
    rules = [{"id": "chain", "when": {"from": {"relation": "chain"}}, "then": {"importance": 30, "tags": ["chain"]}},
             {"id": "tag", "when": {"source": "chat"}, "then": {"tags": ["chat"]}}]
    by, data = merge(items, rules, agent={"chat1": {"keep": True, "score": 60}, "mail1": {"keep": True, "score": 60},
                                          "other": {"keep": True, "score": 60}})
    assert set(by) == {"mail1", "other"}
    top = by["mail1"]
    assert top["topic"] == "ref:incident:INC-2041" and top["topic_basis"] == "reference"
    assert top["sources"] == ["chat", "mail"] and [m["id"] for m in top["members"]] == ["chat1"]
    assert top["signals"]["tags"] == ["chain", "chat"]
    assert {e["item_id"] for e in top["evidence"]} == {"chat1", "mail1"}
    assert data["merged"] == {"chat1": "mail1"} and data["counts"]["merged"] == 1
    assert top["members"][0]["trace"][-1] == {"stage": "merge", "step": "merged_into", "into": "mail1",
                                              "topic": "ref:incident:INC-2041"}


def test_merge_by_thread_and_chained_keys():
    items = [item("a", thread="t/1", text="first"), item("b", thread="t/1", text="INC-7 noted"),
             item("c", source="mail", text="about INC-7"), item("d", thread="t/2")]
    by, _ = merge(items)
    tops = {k: sorted([k] + [m["id"] for m in v["members"]]) for k, v in by.items()}
    assert sorted(tops.values()) == [["a", "b", "c"], ["d"]]
    assert by["d"]["topic"] == "thread:chat:t/2"


def test_must_surface_item_is_never_dropped_by_the_agent_or_the_floor():
    rules = [{"id": "must", "when": {"text": "(?i)urgent"}, "then": {"surface": "always", "reason": "Urgent"}}]
    by, _ = merge([item("a", text="urgent thing")], rules, agent={"a": {"keep": False, "score": 0}},
                  scoring={"floor": 90})
    assert by["a"]["keep"] and by["a"]["score"] == 90 and by["a"]["signals"]["reasons"] == ["Urgent"]


def test_dismissed_items_are_dropped_with_the_rule_and_not_assessed():
    rules = [{"id": "noise", "when": {"text": "lunch"}, "then": {"dismiss": "Social chatter"}}]
    by, _ = merge([item("a", text="lunch at noon")], rules)
    assert by["a"]["status"] == "dropped" and by["a"]["reason"] == "dismissed: Social chatter"
    assert {"stage": "assess", "step": "dismissed", "rules": ["noise"]} in by["a"]["trace"]


# --- suppression ------------------------------------------------------------------------


def disp(decision, ref, **kw):
    return {"topic_id": ref, "decision": decision, "run_id": "earlier", "item": ref, "until": None, "note": None,
            "recorded_at": "2026-10-14T18:00:00+00:00", **kw}


def test_suppression_paths():
    th = "t/9"
    items = [
        item("dis", text="dismissed forever"),
        item("until-live", text="dismiss until later"), item("until-past", text="dismiss expired"),
        item("watch-quiet", at="2026-10-14T10:00:00+00:00", text="watched, quiet"),
        item("watch-new", at="2026-10-15T10:00:00+00:00", text="watched, new activity"),
        item("done", text="done topic"),
        item("q1", thread=th, handle="u/300", at="2026-10-15T09:00:00+00:00", text="question"),
        item("q2", thread=th, handle="u/100", at="2026-10-15T10:00:00+00:00", text="my answer"),
        item("trk", text="see INC-55 for details"),
        item("free", text="nothing applies"),
    ]
    rows = [disp("dismiss", "dis"), disp("dismiss", "until-live", until="2026-10-20"),
            disp("dismiss", "until-past", until="2026-10-15"), disp("watch", "watch-quiet"),
            disp("watch", "watch-new"), disp("done", "thread:chat:none"), disp("done", "done"),
            disp("done", "free", run_id="this-run")]
    stores = [{"destination": "backlog", "path": "Backlog.md", "open": ["- [ ] Chase INC-55 [source:: chat]"]}]
    by, data = merge(items, dispositions=rows, task_stores=stores, run_date="2026-10-15", run_id="this-run")
    status = {k: (v["status"], v["reason"]) for k, v in by.items()}
    assert status["dis"] == ("suppressed", "disposition: dismiss")
    assert status["until-live"] == ("suppressed", "disposition: dismiss until 2026-10-20")
    assert status["until-past"][0] == "kept"
    assert status["watch-quiet"] == ("suppressed", "disposition: watch, no new activity")
    assert status["watch-new"][0] == "kept"
    assert status["done"] == ("suppressed", "disposition: done")
    assert status["q1"] == ("suppressed", "answered: the latest record is the operator's")
    assert status["trk"] == ("tracked", "tracked in backlog") and by["trk"]["signals"]["route"] == "verify"
    assert by["trk"]["tracked"]["line"] == "- [ ] Chase INC-55 [source:: chat]"
    assert status["free"][0] == "kept"  # a disposition from the run itself is not read back
    assert {r["item_id"] for r in data["suppressed"]} == {"dis", "until-live", "watch-quiet", "done", "q1"}
    assert [t["item_id"] for t in data["tracked"]] == ["trk"]


# --- coverage ---------------------------------------------------------------------------


def test_coverage_cross_reference_miss_and_hit():
    rules = [{"id": "inc", "when": {"reference": "incident"}, "then": {"expect_in": {"source": "wiki",
                                                                                     "reference": "incident"}}}]
    items = [item("c1", text="INC-100 again"), item("c2", text="INC-200 too"),
             item("w1", source="wiki", title="Postmortem INC-200")]
    _, data = merge(items, rules, sources={"wiki": {"outcome": "read"}})
    assert [(n["value"], n["home"], n["item_id"], n["home_outcome"]) for n in data["coverage_notes"]] == \
        [("INC-100", "wiki", "c1", "read")]
    _, data = merge(items[:1], rules)
    assert data["coverage_notes"][0]["home_outcome"] == "not collected"


def test_a_reply_that_promises_something_is_a_commitment_not_an_answer():
    """The operator speaking last usually closes a topic. Not when what they said was a
    promise: "Will do" answers nothing yet, and the assess task says so by keeping it as a
    commitment."""
    ask = item("ask", thread="t1", text="can you ask finance about the renewal?", handle="u/200",
               at="2026-10-15T09:00:00+00:00")
    promise = item("promise", thread="t1", text="Will do", handle="u/100", **{"from": "Rae Holm"},
                   at="2026-10-15T09:05:00+00:00")
    agent = {"ask": {"keep": True, "score": 80, "kind": "request"},
             "promise": {"keep": True, "score": 80, "kind": "commitment", "owner": "me"}}
    by, _ = merge([ask, promise], agent=agent)
    (it,) = by.values()
    assert it["status"] != "suppressed" and it["keep"] is True
    assert any(t.get("step") == "open_commitment" for t in it["trace"])

    done = {"ask": {"keep": True, "score": 80, "kind": "request"}, "promise": {"keep": False, "score": 5}}
    by, _ = merge([ask, {**promise, "text": "Done, they said yes"}], agent=done)
    (it,) = by.values()
    assert it["status"] == "suppressed" and it["reason"].startswith("answered")

    theirs = {"ask": {"keep": True, "score": 80, "kind": "commitment", "owner": "vic"},   # someone else's promise
              "promise": {"keep": False, "score": 5}}
    by, _ = merge([ask, promise], agent=theirs)
    assert next(iter(by.values()))["status"] == "suppressed"


def test_the_assess_output_hands_merge_the_kind_of_each_kept_item():
    from docket.signals.node import agent_rows
    # ``assessment`` is where the assess stage puts the accepted row (tasks/runner.py: combine_assess).
    rows = agent_rows([{"id": "a", "keep": True, "score": 80, "assessment": {"kind": "commitment", "topic": "renewal"}},
                       {"id": "b", "keep": False, "score": 5, "assess": {}}, {"id": "c"}])
    assert rows == {"a": {"keep": True, "score": 80, "kind": "commitment", "topic": "renewal"},
                    "b": {"keep": False, "score": 5}}


def test_an_acknowledged_topic_is_quiet_until_something_new_is_said():
    """Acknowledge is a decision of its own: seen, nothing owed. Like watch it suppresses the
    topic only while nothing new has happened, and it says so by name."""
    ask = item("a", thread="t1", text="for your awareness: the migration finished", handle="u/200",
               at="2026-10-15T09:00:00+00:00")
    seen = [{"topic_id": "thread:chat:t1", "decision": "ack", "run_id": "earlier", "item": "a",
             "recorded_at": "2026-10-15T10:00:00+00:00"}]
    agent = {"a": {"keep": True, "score": 80}}
    by, _ = merge([ask], agent=agent, dispositions=seen)
    assert by["a"]["status"] == "suppressed" and by["a"]["reason"] == "disposition: ack, no new activity"
    later = item("b", thread="t1", text="correction: two tables are missing", handle="u/200",
                 at="2026-10-15T11:00:00+00:00")
    by, _ = merge([ask, later], agent={**agent, "b": {"keep": True, "score": 80}}, dispositions=seen)
    assert next(iter(by.values()))["status"] != "suppressed"


def test_an_item_whose_key_is_on_an_open_task_line_is_tracked():
    """A task line written for an item carries its ledger key. The item is then tracked,
    not put forward again, even when its link is not on the line."""
    from docket.items.ledger import item_key
    ask = item("a", thread="t9", text="please review the plan", handle="u/200")
    key = item_key("thread:chat:t9")
    stores = [{"destination": "backlog", "path": "tasks.md", "open": [f"- [ ] Review the plan [id:: {key}]"]}]
    by, _ = merge([ask], agent={"a": {"keep": True, "score": 90}}, task_stores=stores)
    assert by["a"]["status"] == "tracked" and by["a"]["tracked"]["match"] == key
    by, _ = merge([ask], agent={"a": {"keep": True, "score": 90}},
                  task_stores=[{"destination": "backlog", "path": "tasks.md", "open": ["- [ ] Something else [id:: iother123]"]}])
    assert by["a"]["status"] != "tracked"
