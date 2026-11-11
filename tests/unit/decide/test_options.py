from __future__ import annotations

import pytest

from docket.cli.errors import DocketError
from docket.decide.options import resolve_option

pytestmark = pytest.mark.smoke
OPTS = [
    {"id": "task", "label": "Add to backlog (Mon)", "destination": "backlog"},
    {"id": "agenda", "label": "Raise at Platform Sync", "destination": "agendas"},
    {"id": "block", "label": "Block 1h this week"},
    {"id": "drop", "label": "Dismiss"},
    {"id": "draft", "label": "Draft a reply"},
]


@pytest.mark.parametrize("text,expected", [
    ("task", "task"), ("TASK", "task"), ("2", "agenda"), ("Dismiss", "drop"), ("raise at", "agenda"),
    ("add to backlog", "task"), ("Add to backlog (Mon)", "task"), ("agendas", "agenda"), ("block 1h", "block"),
    ("draft a", "draft"), ("dism", "drop"),
])
def test_resolves(text, expected):
    assert resolve_option(text, OPTS)["id"] == expected


def test_ambiguous_lists_candidates():
    with pytest.raises(DocketError) as ei:
        resolve_option("dr", OPTS, question_id="q_1")
    assert ei.value.code == "input.ambiguous_option"
    assert {c["id"] for c in ei.value.details["candidates"]} == {"drop", "draft"}


@pytest.mark.parametrize("text", ["nothing", "9", "", "0"])
def test_unknown(text):
    with pytest.raises(DocketError) as ei:
        resolve_option(text, OPTS)
    assert ei.value.code == "input.unknown_option"


def test_the_item_view_carries_the_words_the_assess_task_quoted():
    """A task line or a card can show the sentence that made the item a finding, which is
    rarely the first sentence of the message."""
    from types import SimpleNamespace
    from docket.decide.options import item_view
    from tests.task_helpers import persona_config
    rc = SimpleNamespace(ws=SimpleNamespace(config=persona_config("reference-like")))
    it = {"id": "i1", "source": "chat", "title": "@all a long preamble", "text": "@all a long preamble\nWho owns this?",
          "assessment": {"ask": "Name the owner", "evidence": [{"line": "L2", "quote": "Who owns this?"},
                                                               {"line": "L1", "quote": "@all a long preamble"}]}}
    v = item_view(rc, it)
    assert v["quote"] == "Who owns this?" and v["ask"] == "Name the owner"
    assert item_view(rc, {"id": "i2", "source": "chat", "title": "t"})["quote"] == ""
    assert item_view(rc, {"id": "i3", "source": "chat", "assessment": {"evidence": ["plain words"]}})["quote"] == "plain words"


def _rc(tmp_path, units=()):
    import sqlite3
    from types import SimpleNamespace
    from docket.store.db import open_db
    from tests.task_helpers import persona_config
    db = open_db(tmp_path / "d.db")
    db.row_factory = sqlite3.Row
    for source, uid, name in units:
        db.execute("INSERT INTO units (source, unit_id, name) VALUES (?,?,?)", (source, uid, name))
    cfg = persona_config("reference-like")
    return SimpleNamespace(ws=SimpleNamespace(config=cfg), run=SimpleNamespace(store=SimpleNamespace(db=db)))


def test_the_area_comes_from_the_unit_before_the_text(tmp_path):
    """A message in a service's own room belongs to that service whatever it mentions; outside
    such a room the text decides, and only when it points one way."""
    from docket.decide.options import area_for
    rc = _rc(tmp_path, [("chat", "room-ingest", "Ingest Service (Team)"), ("chat", "room-misc", "Hallway")])
    topics = {t.id: t.area for t in rc.ws.config.profile.topics}
    assert topics["ingest"] == "Platform"
    in_room = {"source": "chat", "unit": "room-ingest", "mentioned": {"topics": []}}
    assert area_for(rc, in_room) == "Platform"
    assert area_for(rc, {"source": "chat", "unit": "room-misc", "mentioned": {"topics": ["ingest"]}}) == "Platform"
    assert area_for(rc, {"source": "chat", "unit": "room-misc", "mentioned": {"topics": []}}) == ""
    assert area_for(rc, {**in_room, "signals": {"area": "Finance"}}) == "Finance"   # a rule's word is final


def test_the_item_view_names_the_source_as_configured(tmp_path):
    from docket.decide.options import item_view
    rc = _rc(tmp_path)
    rc.ws.config.sources["chat"].label = "chat thread"
    assert item_view(rc, {"id": "i", "source": "chat"})["source_label"] == "chat thread"
    assert item_view(rc, {"id": "i", "source": "mail"})["source_label"] == "mail"      # no label: the id


def test_a_must_surface_finding_can_be_asked_whatever_the_budget():
    """The budget limits how many findings are asked. A finding a rule marked must-surface
    sits at the floor by construction, so ranking by score alone cuts exactly the ones a
    rule insisted on. An ask node can ask them first, inside or outside the budget."""
    from docket.decide.questions import select_asked

    def f(i, score, surface=False, keep=True):
        return {"id": i, "keep": keep, "score": score, "signals": {"surface": surface}}
    items = [f("a", 70), f("b", 65), f("c", 60), f("must1", 55, surface=True), f("must2", 55, surface=True),
             f("low", 40), f("dropped", 90, keep=False)]
    ids = lambda pair: ([it["id"] for it in pair[0]], [it["id"] for it in pair[1]])  # noqa: E731
    assert ids(select_asked(items, 55, 2)) == (["a", "b"], ["c", "must1", "must2"])                 # ranked
    assert ids(select_asked(items, 55, 3, "first")) == (["must1", "must2", "a"], ["b", "c"])
    assert ids(select_asked(items, 55, 2, "bypass")) == (["must1", "must2", "a", "b"], ["c"])
    assert ids(select_asked(items, 55, 0, "bypass")) == (["must1", "must2"], ["a", "b", "c"])
    assert ids(select_asked(items, 55, None, "bypass")) == (["must1", "must2", "a", "b", "c"], [])


def test_a_question_carries_the_decision_recommended_for_its_item(tmp_path):
    """Every item gets a recommended decision. It is the one the assess task chose when that
    is on offer; an item kept only because a rule insisted takes the configured fallback."""
    from docket.decide.options import recommended_option
    offered = [{"id": "task"}, {"id": "watch"}, {"id": "ack"}]
    rec = lambda it, **cfg: recommended_option(it, offered, cfg)  # noqa: E731
    assert rec({"assessment": {"decision": "watch"}}) == "watch"
    assert rec({"assessment": {"decision": "schedule"}}) is None                       # not on offer
    assert rec({"assessment": {"decision": "schedule"}}, default="task") == "task"
    assert rec({"assessment": {}}, default="task") == "task"
    surfaced = {"assessment": {"keep": False}, "signals": {"surface": True}}
    assert rec(surfaced, surfaced="ack", default="task") == "ack"                      # the rule insisted, the task did not keep it
    assert rec({"assessment": {"keep": True, "decision": "task"}, "signals": {"surface": True}}, surfaced="ack") == "task"
    assert rec(surfaced, surfaced="nope") is None


def test_items_recommended_for_a_batch_option_have_their_own_budget():
    """Items shown together (an acknowledge list) must not crowd out the ones asked one by
    one, nor the other way round: each group is ranked and cut on its own."""
    from docket.decide.questions import select_asked

    def f(i, score, decision, surface=False, keep=True):
        return {"id": i, "keep": True, "score": score, "signals": {"surface": surface},
                "assessment": {"keep": keep, "decision": decision}}
    items = [f("a1", 80, "task"), f("a2", 70, "task"), f("a3", 60, "task"),
             f("n1", 75, "ack"), f("n2", 65, "ack"), f("n3", 58, "ack"),
             f("must", 55, None, surface=True, keep=False)]
    kw = dict(must_surface="bypass", batch={"ack"}, batch_budget=2, surfaced="ack")
    asked, over = select_asked(items, 55, 2, **kw)
    assert [it["id"] for it in asked] == ["must", "a1", "a2", "n1", "n2"]
    assert sorted(it["id"] for it in over) == ["a3", "n3"]
    asked, _ = select_asked(items, 55, 2, must_surface="bypass", batch={"ack"}, batch_budget=None, surfaced="ack")
    assert [it["id"] for it in asked] == ["must", "a1", "a2", "n1", "n2", "n3"]
    asked, over = select_asked(items, 55, 2, must_surface="bypass")          # no batch options: as before
    assert [it["id"] for it in asked] == ["must", "a1", "n1"] and len(over) == 4
