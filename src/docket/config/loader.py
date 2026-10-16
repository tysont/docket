"""Configuration loading: YAML with positions, includes, presets, merge, origins (TD §3.1, §3.3).

The result of loading is plain data plus an origin for every path. Validation
(``docket.config.validate``) runs over that data and reports positions from
the origins, so an error in a merged value still names the file and line that
set it.
"""
from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError

from docket.config import presets as preset_mod
from docket.config.defaults import CORE

Path_ = tuple[Any, ...]
WORKSPACE_FILE = "docket.yaml"


@dataclass(frozen=True)
class Origin:
    layer: str  # core | preset | workspace
    file: str | None = None
    line: int | None = None
    column: int | None = None
    preset: str | None = None

    def label(self) -> str:
        if self.layer == "core":
            return "core"
        where = f"{self.file}:{self.line}" if self.line else str(self.file)
        if self.layer == "preset":
            return f"preset:{self.preset}@{where}"
        return f"workspace:{where}"


@dataclass
class Issue:
    code: str
    message: str
    path: str = ""
    file: str | None = None
    line: int | None = None
    column: int | None = None
    suggestion: str | None = None
    expected: str | None = None
    pass_no: int = 1

    def as_dict(self) -> dict[str, Any]:
        d = {
            "code": self.code, "message": self.message, "path": self.path, "file": self.file,
            "line": self.line, "column": self.column, "pass": self.pass_no,
        }
        if self.suggestion is not None:
            d["suggestion"] = self.suggestion
        if self.expected is not None:
            d["expected"] = self.expected
        return d

    def human(self) -> str:
        where = self.file or "<core>"
        if self.line:
            where += f":{self.line}:{self.column or 1}"
        s = f"{where}: {self.code}: {self.message}"
        if self.suggestion:
            s += f" (did you mean {self.suggestion!r}?)"
        return s


def dotted(path: Path_) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + str(p))
    return out


def closest(word: str, choices: list[str]) -> str | None:
    m = difflib.get_close_matches(str(word), [str(c) for c in choices], n=1, cutoff=0.6)
    return m[0] if m else None


@dataclass
class Layered:
    data: dict[str, Any]
    origins: dict[Path_, Origin]


# --- YAML with positions ---------------------------------------------------


def _plain(node: Any, path: Path_, origins: dict[Path_, Origin], mk) -> Any:
    if isinstance(node, CommentedMap):
        out: dict[str, Any] = {}
        for k, v in node.items():
            key = str(k)
            try:
                line, col = node.lc.key(k)
                origins[path + (key,)] = mk(line + 1, col + 1)
            except (KeyError, TypeError, AttributeError):
                origins[path + (key,)] = origins.get(path, mk(None, None))
            out[key] = _plain(v, path + (key,), origins, mk)
        return out
    if isinstance(node, CommentedSeq):
        lst: list[Any] = []
        for i, v in enumerate(node):
            try:
                line, col = node.lc.item(i)
                origins[path + (i,)] = mk(line + 1, col + 1)
            except (KeyError, TypeError, AttributeError, IndexError):
                origins[path + (i,)] = origins.get(path, mk(None, None))
            lst.append(_plain(v, path + (i,), origins, mk))
        return lst
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, (datetime, date)):
        return node.isoformat()
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


class YamlFileError(Exception):
    def __init__(self, issue: Issue) -> None:
        super().__init__(issue.message)
        self.issue = issue


def load_yaml(path: Path, rel: str, layer: str, preset: str | None = None) -> Layered:
    yaml = YAML(typ="rt")
    try:
        text = path.read_text(encoding="utf-8")
        node = yaml.load(text)
    except MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        raise YamlFileError(Issue(
            "config.yaml_syntax", f"{e.problem or e.context or 'YAML error'}".strip(),
            file=rel, line=(mark.line + 1) if mark else None, column=(mark.column + 1) if mark else None,
        )) from None
    except OSError as e:
        raise YamlFileError(Issue("config.include_missing", f"cannot read {rel}: {e.strerror}", file=rel)) from None

    def mk(line: int | None, col: int | None) -> Origin:
        return Origin(layer, rel, line, col, preset)

    origins: dict[Path_, Origin] = {(): mk(1, 1)}
    if node is None:
        return Layered({}, origins)
    data = _plain(node, (), origins, mk)
    if not isinstance(data, dict):
        raise YamlFileError(Issue("config.bad_type", "a configuration file must be a mapping at the top level",
                                  file=rel, line=1, column=1, expected="mapping"))
    return Layered(data, origins)


def core_layer() -> Layered:
    data = copy.deepcopy(CORE)
    origins: dict[Path_, Origin] = {}

    def walk(v: Any, p: Path_) -> None:
        origins[p] = Origin("core")
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, p + (k,))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, p + (i,))

    walk(data, ())
    return Layered(data, origins)


# --- merge -----------------------------------------------------------------

_ABSENT = object()


def _is_form(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and next(iter(v)) in ("extend", "remove")


def _sub_origins(origins: Mapping[Path_, Origin], prefix: Path_) -> dict[Path_, Origin]:
    n = len(prefix)
    return {p: o for p, o in origins.items() if p[:n] == prefix}


def _drop(origins: dict[Path_, Origin], prefix: Path_) -> None:
    for p in list(_sub_origins(origins, prefix)):
        del origins[p]


def _matches_removal(item: Any, removals: list[Any]) -> bool:
    for r in removals:
        if item == r:
            return True
        if isinstance(item, dict) and isinstance(r, str) and item.get("id") == r:
            return True
    return False


def merge_layers(base: Layered, over: Layered, issues: list[Issue]) -> Layered:
    """Deep-merge ``over`` onto ``base``. Maps merge, lists replace, ``{extend}``/``{remove}`` edit lists."""
    origins = dict(base.origins)

    def m(b: Any, o: Any, path: Path_) -> Any:
        if _is_form(o):
            op, vals = next(iter(o.items()))
            org = over.origins.get(path)
            if not isinstance(vals, list):
                issues.append(_issue_at("config.bad_merge_form", f"'{op}' takes a list", path, org))
                return [] if b is _ABSENT else b
            if b is _ABSENT or b is None:
                items: list[Any] = []
            elif isinstance(b, list):
                items = list(b)
            else:
                issues.append(_issue_at("config.bad_merge_form",
                                        f"'{op}' edits a list, but {dotted(path)} is not a list", path, org))
                return b
            if op == "extend":
                start = len(items)
                for p, oo in _sub_origins(over.origins, path + ("extend",)).items():
                    rest = p[len(path) + 1:]
                    if rest:
                        origins[path + (start + rest[0],) + rest[1:]] = oo
                return items + copy.deepcopy(vals)
            keep_idx = [i for i, x in enumerate(items) if not _matches_removal(x, vals)]
            old = _sub_origins(origins, path)
            _drop(origins, path)
            if path in old:
                origins[path] = old[path]
            for new_i, old_i in enumerate(keep_idx):
                for p, oo in old.items():
                    if len(p) > len(path) and p[len(path)] == old_i:
                        origins[path + (new_i,) + p[len(path) + 1:]] = oo
            return [items[i] for i in keep_idx]
        if isinstance(o, dict):
            if not isinstance(b, dict):
                _drop(origins, path)
                b = {}
            res = dict(b)
            for k, v in o.items():
                res[k] = m(b.get(k, _ABSENT), v, path + (k,))
            if path in over.origins:
                origins[path] = over.origins[path]
            return res
        _drop(origins, path)
        origins.update(_sub_origins(over.origins, path))
        return copy.deepcopy(o)

    data = m(base.data, over.data, ())
    return Layered(data, origins)


def _issue_at(code: str, msg: str, path: Path_, org: Origin | None, **kw: Any) -> Issue:
    return Issue(code, msg, path=dotted(path), file=org.file if org else None,
                 line=org.line if org else None, column=org.column if org else None, **kw)


# --- workspace discovery ---------------------------------------------------


def discover_workspace(cwd: Path, explicit: str | None, env: Mapping[str, str]) -> Path | None:
    """``--workspace``, then ``DOCKET_WORKSPACE``, then walk up from ``cwd``."""
    for cand in (explicit, env.get("DOCKET_WORKSPACE")):
        if cand:
            p = Path(cand).expanduser()
            if not p.is_absolute():
                p = cwd / p
            p = p.resolve()
            if p.is_file() and p.name == WORKSPACE_FILE:
                p = p.parent
            return p if (p / WORKSPACE_FILE).is_file() else None
    cur = cwd.resolve()
    for d in [cur, *cur.parents]:
        if (d / WORKSPACE_FILE).is_file():
            return d
    return None


# --- loading ---------------------------------------------------------------


@dataclass
class Loaded:
    root: Path
    data: dict[str, Any]
    origins: dict[Path_, Origin]
    files: list[str]
    presets: list[str]
    issues: list[Issue] = field(default_factory=list)


def _load_workspace_files(root: Path, issues: list[Issue]) -> tuple[Layered, list[str]]:
    files: list[str] = []
    data: dict[str, Any] = {}
    origins: dict[Path_, Origin] = {}
    section_file: dict[str, str] = {}
    queue: list[tuple[str, Origin | None]] = [(WORKSPACE_FILE, None)]
    seen: set[str] = set()
    while queue:
        rel, inc_org = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        path = (root / rel)
        if not path.is_file():
            issues.append(Issue("config.include_missing", f"included file {rel!r} does not exist",
                                path="includes", file=inc_org.file if inc_org else rel,
                                line=inc_org.line if inc_org else None,
                                column=inc_org.column if inc_org else None))
            continue
        try:
            lay = load_yaml(path, rel, "workspace")
        except YamlFileError as e:
            issues.append(e.issue)
            continue
        files.append(rel)
        for key, val in lay.data.items():
            if key in section_file:
                o = lay.origins.get((key,))
                issues.append(Issue(
                    "config.duplicate_section",
                    f"section {key!r} is already defined in {section_file[key]}; define each section in one file",
                    path=key, file=rel, line=o.line if o else None, column=o.column if o else None))
                continue
            section_file[key] = rel
            data[key] = val
            origins.update(_sub_origins(lay.origins, (key,)))
        incs = lay.data.get("includes", [])
        if isinstance(incs, list):
            for i, inc in enumerate(incs):
                if isinstance(inc, str):
                    queue.append((inc, lay.origins.get(("includes", i))))
    origins[()] = Origin("workspace", WORKSPACE_FILE, 1, 1)
    return Layered(data, origins), files


def load(root: Path) -> Loaded:
    """Load and merge core, presets and workspace files. Never raises on configuration errors."""
    issues: list[Issue] = []
    ws, files = _load_workspace_files(root, issues)
    layered = core_layer()
    preset_ids: list[str] = []
    raw_presets = ws.data.get("presets", [])
    if isinstance(raw_presets, list):
        available = preset_mod.preset_ids()
        for i, pid in enumerate(raw_presets):
            org = ws.origins.get(("presets", i))
            if not isinstance(pid, str):
                continue
            ppath = preset_mod.preset_path(pid)
            if ppath is None:
                issues.append(_issue_at("config.unknown_preset", f"no shipped preset {pid!r}", ("presets", i), org,
                                        suggestion=closest(pid, available)))
                continue
            try:
                lay = load_yaml(ppath, ppath.relative_to(preset_mod.PRESET_ROOT).as_posix(), "preset", pid)
            except YamlFileError as e:
                issues.append(e.issue)
                continue
            if preset_mod.scope_of(lay.data) == "source":
                issues.append(_issue_at("config.preset_scope",
                                        f"preset {pid!r} is a source preset: name it in sources.<id>.extends, "
                                        "not in top-level presets", ("presets", i), org))
                continue
            for fp in preset_mod.FORBIDDEN:
                cur: Any = lay.data
                for k in fp:
                    cur = cur.get(k, _ABSENT) if isinstance(cur, dict) else _ABSENT
                if cur is not _ABSENT:
                    issues.append(_issue_at("config.preset_forbidden",
                                            f"preset {pid!r} defines {dotted(fp)}, which only a workspace may define",
                                            fp, lay.origins.get(fp)))
            lay.data.pop("presets", None)
            lay.data.pop(preset_mod.META_KEY, None)
            layered = merge_layers(layered, lay, issues)
            preset_ids.append(pid)
    layered = merge_layers(layered, ws, issues)
    from docket.config.source_checks import apply_extends  # source presets (TD §6.1)
    layered = apply_extends(layered, issues)
    return Loaded(root, layered.data, layered.origins, files, preset_ids, issues)


def origin_for(origins: Mapping[Path_, Origin], path: Path_) -> Origin | None:
    p = tuple(path)
    while True:
        if p in origins:
            return origins[p]
        if not p:
            return None
        p = p[:-1]
