"""Scoring, floors, caps and budgets (TD §8.2, FR-SIG-04, FR-SIG-05).

Components, each clamped to 0..100:

- ``importance`` and ``urgency``: ``base`` plus the rule contributions;
- ``agent``: the accepted assess result's ``score``. Items assessed by rules alone
  (``assess: rules_only``) or not assessed have no agent component.

``weighted``: ``sum(w_k * c_k) / sum(w_k)`` over the components present, so the
configured weights apply unchanged when every component is present and a
rules-only item is scored on its rule components alone. ``max``: the largest
component with a positive weight. ``plugin``: a registered formula. The result is
rounded half up and clamped to 0..100.

``keep`` decides, in order: ``surface: always`` keeps an item whatever its score,
raised to ``surface_floor`` (default: the floor) so later stages that rank by
score keep it; below ``floor`` drops; at or above ``always_keep`` bypasses caps
and budgets; ``caps.per_source`` keeps the top N per source (``default`` for
sources not named); ``budgets.daily`` keeps the top N across sources. Items that
bypass (must-surface, always-keep) are kept in addition and use no cap or budget
place: "keep N plus every overflow item". Ranking is score descending, then id.
Every decision appends to the item's trace.
"""
from __future__ import annotations

import math
from typing import Any

from docket.shapes.unexpected import unexpected
from docket.signals import plugins
from docket.signals.models import Budgets, Scoring


def clamp(v: float) -> int:
    return max(0, min(100, int(math.floor(v + 0.5))))


def components(sig: dict[str, Any], scoring: Scoring, agent: int | None) -> dict[str, int]:
    out = {"importance": clamp(scoring.base + sig["importance"]), "urgency": clamp(scoring.base + sig["urgency"])}
    if agent is not None:
        out["agent"] = clamp(agent)
    return out


def combine(comp: dict[str, int], scoring: Scoring) -> int:
    w = {k: float(scoring.weights.get(k, 0.0)) for k in comp}  # type: ignore[call-overload]
    if scoring.formula == "plugin":
        fn = plugins.formula(scoring.plugin or "")
        if fn is not None:
            try:
                return clamp(float(fn(dict(comp), w)))
            except Exception as e:
                unexpected("signals.scoring", f"formula plugin {scoring.plugin} raised {type(e).__name__}",
                           sample=str(e)[:200], reading="score with the weighted formula",
                           extend=f"entry point group {plugins.GROUP}: {scoring.plugin}")
        else:
            unexpected("signals.scoring", f"formula plugin {scoring.plugin} is not registered",
                       reading="score with the weighted formula", extend=f"entry point group {plugins.GROUP}")
    if scoring.formula == "max":
        vals = [c for k, c in comp.items() if w[k] > 0]
        return clamp(max(vals)) if vals else 0
    total = sum(w.values())
    if total <= 0:
        return 0
    return clamp(sum(w[k] * comp[k] for k in comp) / total)


def score_item(it: dict[str, Any], scoring: Scoring, agent: int | None) -> None:
    comp = components(it["signals"], scoring, agent)
    it["components"] = comp
    it["score"] = combine(comp, scoring)
    it["trace"].append({"stage": "merge", "step": "score", "formula": scoring.formula,
                        "weights": {k: scoring.weights.get(k, 0.0) for k in comp},  # type: ignore[call-overload]
                        "base": scoring.base, "components": comp, "score": it["score"]})


def rank_key(it: dict[str, Any]) -> tuple[int, str]:
    return (-int(it.get("score") or 0), str(it["id"]))


def decide_keep(cands: list[dict[str, Any]], scoring: Scoring, budgets: Budgets) -> None:
    """Set ``keep``/``status``/``reason`` on candidates (items not already dropped or suppressed)."""
    floor = scoring.floor
    ak = scoring.always_keep
    for it in cands:
        if it["signals"]["surface"]:
            lift = scoring.surface_floor if scoring.surface_floor is not None else floor
            if it["score"] < lift:
                it["trace"].append({"stage": "merge", "step": "surface_floor", "from": it["score"], "to": lift,
                                    "rules": it["signals"]["surface_rules"]})
                it["score"] = lift
    ranked = sorted(cands, key=rank_key)
    per_source: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    for it in ranked:
        s, surface = it["score"], it["signals"]["surface"]
        bypass = surface or (ak is not None and s >= ak)
        if s < floor and not surface:
            _drop(it, "floor", f"score {s} below floor {floor}", floor=floor)
            continue
        cap = scoring.caps.per_source.get(it["source"], scoring.caps.per_source.get("default"))
        pos = per_source.get(it["source"], 0)
        if not bypass:
            per_source[it["source"]] = pos + 1
        if bypass:
            why = "surface: always" if surface else f"score {s} at or above always_keep {ak}"
            it["trace"].append({"stage": "merge", "step": "bypass", "reason": why,
                                **({"rules": it["signals"]["surface_rules"]} if surface else {})})
        elif cap is not None and pos >= cap:
            _drop(it, "cap", f"rank {pos + 1} past the {it['source']} cap {cap}", cap=cap)
            continue
        kept.append(it)
    daily = budgets.daily
    n = 0
    for it in kept:
        s = it["score"]
        bypass = it["signals"]["surface"] or (ak is not None and s >= ak)
        if daily is not None and not bypass and n >= daily:
            _drop(it, "budget", f"past the daily budget {daily}", budget=daily)
            continue
        if not bypass:
            n += 1
        it["keep"], it["status"], it["reason"] = True, "kept", None
        it["trace"].append({"stage": "merge", "step": "kept", "score": s})


def _drop(it: dict[str, Any], step: str, reason: str, **kw: Any) -> None:
    it["keep"], it["status"], it["reason"] = False, "dropped", reason
    it["trace"].append({"stage": "merge", "step": step, "reason": reason, "score": it["score"], **kw})
