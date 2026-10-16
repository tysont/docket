"""Configuration API.

``check(root)`` loads and validates, returning every issue. ``load_workspace(root)``
returns a ``Workspace`` or raises ``config.invalid`` with every issue in
``details.errors``. ``explain(ws, path)`` returns a value and its origin.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docket.cli.errors import ConfigError, InputError
from docket.config.digest import config_digests
from docket.config.loader import Issue, Loaded, Origin, Path_, closest, dotted, load, origin_for
from docket.config.models import DocketConfig, relationship_sets
from docket.config.validate import validate


@dataclass
class Workspace:
    root: Path
    config: DocketConfig
    resolved: dict[str, Any]
    origins: dict[Path_, Origin]
    files: list[str]
    presets: list[str]
    digest: str
    sections: dict[str, str]

    def relationship_sets(self) -> dict[str, list[str]]:
        return relationship_sets(self.config.profile)


def check(root: Path) -> tuple[Workspace | None, list[Issue], Loaded]:
    loaded = load(root)
    model, issues = validate(loaded)
    if model is None:
        return None, issues, loaded
    resolved = model.model_dump(mode="json")
    dg, sections = config_digests(resolved)
    ws = Workspace(root, model, resolved, loaded.origins, loaded.files, loaded.presets, dg, sections)
    return ws, issues, loaded


def invalid_error(issues: list[Issue]) -> ConfigError:
    first = issues[0]
    return ConfigError(
        "config.invalid",
        f"{len(issues)} configuration error(s); first: {first.human()}",
        hint="docket config validate",
        details={"errors": [i.as_dict() for i in issues]},
    )


# A resolved workspace, keyed by its root and the modification time and size of every file it read
# (TD §15.6). Presets ship with the code and never change under a running process. A process that
# runs many commands (the rehearsal loop, an in-process agent, the tests) loads each configuration
# once; any edit to any workspace file misses the cache. Workspaces are never mutated after loading.
_CACHE: dict[str, tuple[tuple[Any, ...], Workspace]] = {}
_CACHE_MAX = 16


def _stamp(root: Path, files: list[str]) -> tuple[Any, ...]:
    out = []
    for rel in files:
        try:
            st = (root / rel).stat()
            out.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((rel, None, None))
    return tuple(out)


def load_workspace(root: Path) -> Workspace:
    key = str(root)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == _stamp(root, hit[1].files):
        return hit[1]
    ws, issues, _ = check(root)
    if ws is None:
        raise invalid_error(issues)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (_stamp(root, ws.files), ws)
    return ws


def _parse_path(path: str) -> Path_:
    out: list[Any] = []
    for part in path.replace("]", "").replace("[", ".").split("."):
        if part == "":
            continue
        out.append(int(part) if part.isdigit() else part)
    return tuple(out)


def explain(ws: Workspace, path: str) -> dict[str, Any]:
    """Value at a dotted path, with the origin of the value and of each child."""
    p = _parse_path(path)
    cur: Any = ws.resolved
    for i, el in enumerate(p):
        if isinstance(cur, dict) and str(el) in cur:
            cur = cur[str(el)]
        elif isinstance(cur, list) and isinstance(el, int) and 0 <= el < len(cur):
            cur = cur[el]
        else:
            choices = list(cur) if isinstance(cur, dict) else []
            raise InputError(
                "input.unknown_path", f"no configuration at {dotted(p[: i + 1])!r}",
                hint="docket config explain <section> lists a section's keys",
                details={"suggestion": closest(str(el), choices), "keys": choices[:50]},
            )
    norm = tuple(str(x) if not isinstance(x, int) else x for x in p)
    org = ws.origins.get(norm)
    # A value with no recorded origin is a model default, which is part of core.
    label = org.label() if org else "core"
    out: dict[str, Any] = {"path": dotted(norm), "value": cur, "origin": label}
    if isinstance(cur, dict):
        out["children"] = {
            k: (ws.origins[norm + (k,)].label() if norm + (k,) in ws.origins else "core") for k in cur
        }
    return out
