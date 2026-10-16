"""Scoring (TD §8.2, FR-SIG-04, FR-SIG-05): the formula, clamping, floor, always_keep, caps, budgets."""
from __future__ import annotations

import pytest

from docket.signals import plugins
from docket.signals.merge import MergeInputs, build_merge
from docket.signals.models import Scoring, parse
from docket.signals.scoring import combine, components
from tests.signal_helpers import env, item


def sig(importance=0, urgency=0):
    return {"importance": importance, "urgency": urgency}


@pytest.mark.parametrize("scoring,s,agent,expected", [
    ({}, sig(), None, 50),                                                       # neutral defaults
    ({}, sig(), 80, 60),                                                         # (50 + 50 + 80) / 3
    ({"weights": {"importance": 0.3, "urgency": 0.4, "agent": 0.3}}, sig(30, 10), 70, 69),  # 24 + 24 + 21
    ({"weights": {"importance": 0.3, "urgency": 0.4, "agent": 0.3}}, sig(30, 10), None, 69),  # rules-only renormalizes
    ({"base": 40}, sig(100, 100), None, 100),                                    # components clamp at 100
    ({}, sig(-80, -80), 0, 0),                                                    # and at 0
    ({"formula": "max"}, sig(10, 30), 40, 80),
    ({"formula": "max", "weights": {"importance": 1, "urgency": 0, "agent": 1}}, sig(10, 30), 40, 60),
    ({"weights": {"importance": 1, "urgency": 1, "agent": 0}}, sig(), 100, 50),  # a zero weight ignores agent
    ({"weights": {"importance": 1, "urgency": 0, "agent": 0}}, sig(5), None, 55),
    ({"weights": {"importance": 1, "urgency": 1, "agent": 1}}, sig(1, 0), None, 51),  # 50.5 rounds half up
])
def test_formula(scoring, s, agent, expected):
    sc = Scoring.model_validate(scoring)
    assert combine(components(s, sc, agent), sc) == expected


def test_plugin_formula_and_its_fallback():
    plugins.register_formula("double_agent", lambda comp, w: comp.get("agent", 0) * 2)
    try:
        sc = Scoring.model_validate({"formula": "plugin", "plugin": "double_agent"})
        assert combine(components(sig(), sc, 30), sc) == 60
        assert combine(components(sig(), sc, 70), sc) == 100
    finally:
        plugins.unregister("double_agent")
    sc = Scoring.model_validate({"formula": "plugin", "plugin": "double_agent"})
    assert combine(components(sig(), sc, 80), sc) == 60  # not registered: weighted, and a shape is recorded


def test_weights_must_be_finite_and_one_positive():
    with pytest.raises(Exception):
        Scoring.model_validate({"weights": {"importance": float("inf")}})
    with pytest.raises(Exception):
        Scoring.model_validate({"weights": {"importance": 0, "urgency": 0, "agent": 0}})


def _merge(items, rules=(), scoring=None, budgets=None, agent=None):
    e = env(list(rules), scoring=scoring, budgets=budgets)
    data = build_merge(MergeInputs(items=items, env=e, agent=agent or {}))
    return {it["id"]: it for it in data["items"]}, data


def _imp(n):
    return {"id": f"imp{n}", "when": {"field": {"name": "imp", "equals": n}}, "then": {"importance": n}}


def scored(n_items, source="chat", start=0):
    """Items whose importance contribution is their index, rules-only so the score is predictable."""
    return [item(f"{source}{i:02d}", source=source, fields={"imp": i}) for i in range(start, start + n_items)]


RULES = [_imp(i) for i in range(0, 60)] + [{"id": "ro", "when": {"source": ["chat", "mail"]},
                                            "then": {"assess": "rules_only"}}]
WEIGHTS = {"weights": {"importance": 1, "urgency": 0, "agent": 0}}


def test_floor_drops_and_must_surface_bypasses_the_floor():
    rules = RULES + [{"id": "must", "when": {"field": {"name": "imp", "equals": 1}}, "then": {"surface": "always"}}]
    by, _ = _merge(scored(10), rules, scoring={**WEIGHTS, "floor": 55})
    assert by["chat05"]["status"] == "kept" and by["chat04"]["status"] == "dropped"
    assert by["chat04"]["reason"] == "score 54 below floor 55"
    assert by["chat01"]["status"] == "kept" and by["chat01"]["score"] == 55  # raised to the floor (surface_floor)
    steps = [e["step"] for e in by["chat01"]["trace"]]
    assert "surface_floor" in steps and "bypass" in steps


def test_surface_floor_setting():
    rules = RULES + [{"id": "must", "when": {"field": {"name": "imp", "equals": 1}}, "then": {"surface": "always"}}]
    by, _ = _merge(scored(3), rules, scoring={**WEIGHTS, "floor": 55, "surface_floor": 75})
    assert by["chat01"]["score"] == 75


def test_per_source_caps_keep_the_top_n_and_always_keep_bypasses():
    items = scored(10, "chat", 30) + scored(5, "mail", 20)
    by, data = _merge(items, RULES, scoring={**WEIGHTS, "always_keep": 88,
                                             "caps": {"per_source": {"default": 3, "mail": 2}}})
    kept = sorted(k for k, v in by.items() if v["keep"])
    # chat 30..39 scores 80..89: 89 and 88 always keep (no cap place), then the top 3 of the rest
    assert [k for k in kept if k.startswith("chat")] == ["chat35", "chat36", "chat37", "chat38", "chat39"]
    assert [k for k in kept if k.startswith("mail")] == ["mail23", "mail24"]
    assert by["chat34"]["reason"] == "rank 4 past the chat cap 3"
    assert [e["step"] for e in by["chat39"]["trace"]][-2:] == ["bypass", "kept"]
    assert data["counts"]["kept"] == 7


def test_daily_budget_counts_only_competing_items():
    rules = RULES + [{"id": "must", "when": {"field": {"name": "imp", "equals": 2}}, "then": {"surface": "always"}}]
    by, _ = _merge(scored(10), rules, scoring=WEIGHTS, budgets={"daily": 3})
    assert sorted(k for k, v in by.items() if v["keep"]) == ["chat02", "chat07", "chat08", "chat09"]
    assert by["chat06"]["reason"] == "past the daily budget 3"


def test_budget_in_scoring_is_accepted_and_signals_budgets_wins():
    cfg = parse({"scoring": {"budget": {"questions": 5, "review_pool": 9}}, "budgets": {"questions": 7}})
    assert cfg.effective_budgets.model_dump() == {"questions": 7, "review_pool": 9, "daily": None}


def test_agent_component_and_keep_false():
    items = [item("a"), item("b"), item("c")]
    by, _ = _merge(items, agent={"a": {"keep": True, "score": 90}, "b": {"keep": False, "score": 90}},
                   scoring={"floor": 50})
    assert by["a"]["components"] == {"importance": 50, "urgency": 50, "agent": 90} and by["a"]["score"] == 63
    assert by["b"]["status"] == "dropped" and by["b"]["reason"] == "assess: the agent kept nothing"
    assert by["c"]["agent_pending"] and "agent" not in by["c"]["components"]
