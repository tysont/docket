"""Plugin predicates and scoring formulas (TD §8.1 ``plugin``, §8.2 ``formula: plugin``, §17.1).

Two ways in:

- in process: ``register_predicate(name, fn)`` and ``register_formula(name, fn)``
  (tests, embedding applications);
- installed packages: entry points in the group ``docket.plugins`` whose loaded
  object has a ``DOCKET_PREDICATES`` and/or ``DOCKET_FORMULAS`` mapping of name to
  callable. Entry points are read once, on first lookup of an unknown name, so
  workspaces that use no plugin never pay for the scan.

A predicate is ``fn(item: dict, args: dict) -> bool`` over the enriched item
(``sender``, ``mentioned``, ``references``, ``fields``...). A formula is
``fn(components: dict[str, int], weights: dict[str, float]) -> float``.
"""
from __future__ import annotations

from typing import Any, Callable

Predicate = Callable[[dict[str, Any], dict[str, Any]], bool]
Formula = Callable[[dict[str, int], dict[str, float]], float]

_PREDICATES: dict[str, Predicate] = {}
_FORMULAS: dict[str, Formula] = {}
_LOADED = False
GROUP = "docket.plugins"


def register_predicate(name: str, fn: Predicate) -> None:
    _PREDICATES[name] = fn


def register_formula(name: str, fn: Formula) -> None:
    _FORMULAS[name] = fn


def unregister(name: str) -> None:
    _PREDICATES.pop(name, None)
    _FORMULAS.pop(name, None)


def _load_entry_points() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from importlib.metadata import entry_points
    try:
        eps = list(entry_points(group=GROUP))
    except Exception:  # a broken environment is not a broken workspace
        return
    for ep in eps:
        try:
            obj = ep.load()
        except Exception:
            continue
        for name, fn in (getattr(obj, "DOCKET_PREDICATES", None) or {}).items():
            _PREDICATES.setdefault(str(name), fn)
        for name, fn in (getattr(obj, "DOCKET_FORMULAS", None) or {}).items():
            _FORMULAS.setdefault(str(name), fn)


def predicate(name: str) -> Predicate | None:
    if name not in _PREDICATES:
        _load_entry_points()
    return _PREDICATES.get(name)


def formula(name: str) -> Formula | None:
    if name not in _FORMULAS:
        _load_entry_points()
    return _FORMULAS.get(name)


def names() -> dict[str, list[str]]:
    _load_entry_points()
    return {"predicates": sorted(_PREDICATES), "formulas": sorted(_FORMULAS)}
