"""Source presets and the validation of ``sources``, ``transports`` and ``signals.units``.

``apply_extends`` runs inside the loader: a source with ``extends: <preset id>``
merges the preset's ``source:`` template beneath its own keys, keeping the
origin of every value, so ``config explain`` names the preset line a value came
from. A ``files`` source with a ``path`` and no ``extends`` extends ``files/jsonl``.

``check`` runs passes 2 to 4 of TD §3.4 for these sections, reading the merged
data defensively. It never contacts a system.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

from pydantic import ValidationError

from docket.config import presets as preset_mod
from docket.config.loader import (
    Issue, Layered, Loaded, Path_, YamlFileError, _sub_origins, closest, dotted, load_yaml, merge_layers, origin_for,
)
from docket.config.models import MAPPING_KEYS, SOURCE_KINDS
from docket.config.source_models import UnitSignals

FILES_PRESET = "files/jsonl"
_CRED_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|credential|bearer|authorization|cookie)", re.I)
_SCHEME = re.compile(r"^(bearer|basic|token)\s+", re.I)


# --- extends -------------------------------------------------------------------


_CACHE: dict[tuple[str, int, int], Layered] = {}


def _load_preset(ppath: Any, pid: str) -> Layered:
    """A source preset's layer, parsed once per process per file version (YAML parsing dominates load time)."""
    import copy
    st = ppath.stat()
    key = (str(ppath), st.st_mtime_ns, st.st_size)
    if key not in _CACHE:
        _CACHE[key] = load_yaml(ppath, ppath.relative_to(preset_mod.PRESET_ROOT).as_posix(), "preset", pid)
    lay = _CACHE[key]
    return Layered(copy.deepcopy(lay.data), dict(lay.origins))


def _preset_id(ref: str) -> str:
    return ref[len("preset:"):] if ref.startswith("preset:") else ref


def apply_extends(layered: Layered, issues: list[Issue]) -> Layered:
    sources = layered.data.get("sources")
    if not isinstance(sources, dict):
        return layered
    for sid, src in list(sources.items()):
        if not isinstance(src, dict):
            continue
        ref = src.get("extends")
        if ref is None and src.get("kind") == "files" and src.get("path") and not src.get("transport"):
            ref = FILES_PRESET
        if not isinstance(ref, str):
            continue
        pid = _preset_id(ref)
        base: Path_ = ("sources", sid)
        org = layered.origins.get(base + ("extends",)) or layered.origins.get(base)
        ppath = preset_mod.preset_path(pid)
        if ppath is None:
            issues.append(Issue("config.unknown_preset", f"source {sid!r} extends {ref!r}, which is not a shipped preset",
                                path=dotted(base + ("extends",)), file=org.file if org else None,
                                line=org.line if org else None, column=org.column if org else None,
                                suggestion=closest(pid, preset_mod.preset_ids())))
            continue
        try:
            lay = _load_preset(ppath, pid)
        except YamlFileError as e:
            issues.append(e.issue)
            continue
        tmpl = lay.data.get("source")
        if not isinstance(tmpl, dict):
            issues.append(Issue("config.unknown_preset", f"preset {pid!r} is not a source preset (it has no 'source:')",
                                path=dotted(base + ("extends",)), file=org.file if org else None,
                                line=org.line if org else None))
            continue
        t_origins = {base + p[1:]: o for p, o in lay.origins.items() if p[:1] == ("source",)}
        t_layer = Layered({"sources": {sid: tmpl}}, t_origins)
        i_layer = Layered({"sources": {sid: src}}, _sub_origins(layered.origins, base))
        merged = merge_layers(t_layer, i_layer, issues)
        sources[sid] = merged.data["sources"][sid]
        for p in list(_sub_origins(layered.origins, base)):
            del layered.origins[p]
        layered.origins.update(_sub_origins(merged.origins, base))
        if src.get("extends") is None:
            sources[sid]["extends"] = pid
    return layered


# --- helpers -------------------------------------------------------------------


def _issue(loaded: Loaded, code: str, msg: str, path: Path_, pass_no: int, **kw: Any) -> Issue:
    org = origin_for(loaded.origins, path)
    return Issue(code, msg, path=dotted(path), file=org.file if org else None,
                 line=org.line if org else None, column=org.column if org else None, pass_no=pass_no, **kw)


def _items(v: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(v, dict):
        yield from v.items()


def _get(d: Any, *keys: Any) -> Any:
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def looks_secret(value: str) -> bool:
    """A high-entropy literal: at least 16 characters with no spaces, at least 3.2 bits
    per character, and letters mixed with digits (or 32+ characters), after an optional ``Bearer``/``Basic``/``Token`` scheme. A scheme followed
    by any 8+ character credential also counts."""
    v = value.strip()
    m = _SCHEME.match(v)
    if m:
        rest = v[m.end():].strip()
        return len(rest) >= 8 and " " not in rest
    if len(v) < 16 or any(c.isspace() for c in v) or "{" in v:
        return False
    mixed = any(c.isdigit() for c in v) and any(c.isalpha() for c in v)
    return entropy(v) >= 3.2 and (mixed or len(v) >= 32)


def _secret_literals(loaded: Loaded, data: Any, path: Path_, out: list[Issue]) -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            p = path + (k,)
            if isinstance(v, str) and _CRED_KEY.search(str(k)) and looks_secret(v):
                out.append(_issue(loaded, "config.secret_literal",
                                  f"{dotted(p)} holds what looks like a secret; reference it instead with "
                                  f"{{ env: VARIABLE }} or {{ file: PATH }}", p, 3))
            else:
                _secret_literals(loaded, v, p, out)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _secret_literals(loaded, v, path + (i,), out)


def _regex(loaded: Loaded, rx: Any, path: Path_, out: list[Issue]) -> None:
    if not isinstance(rx, str):
        return
    try:
        re.compile(rx)
    except re.error as e:
        out.append(_issue(loaded, "config.bad_regex", f"{dotted(path)}: {e}", path, 3))


def source_kind(src: dict[str, Any], transports: dict[str, Any]) -> str | None:
    tname = src.get("transport")
    if isinstance(tname, str) and isinstance(transports.get(tname), dict):
        k = transports[tname].get("kind")
        return k if isinstance(k, str) else None
    k = src.get("kind")
    return k if isinstance(k, str) else None


_TEMPLATE_NEEDS = {
    "exec_jsonl": ("op", "an op"), "mcp_stdio": ("tool", "a tool"), "mcp_http": ("tool", "a tool"),
    "codemode": ("script", "a script"), "files": ("path", "a path"),
}


# --- passes ----------------------------------------------------------------------


def check(loaded: Loaded) -> list[Issue]:
    d = loaded.data
    out: list[Issue] = []
    transports = d.get("transports") if isinstance(d.get("transports"), dict) else {}
    sources = d.get("sources") if isinstance(d.get("sources"), dict) else {}

    # pass 2: references
    for sid, src in _items(sources):
        if not isinstance(src, dict):
            continue
        t = src.get("transport")
        if isinstance(t, str) and t not in transports:
            out.append(_issue(loaded, "config.bad_reference",
                              f"sources.{sid}.transport names transport {t!r}, which is not defined under transports",
                              ("sources", sid, "transport"), 2, suggestion=closest(t, list(transports))))
    units_raw = _get(d, "signals", "units")
    if units_raw is not None:
        try:
            UnitSignals.model_validate(units_raw)
        except ValidationError as e:
            for err in e.errors():
                loc = ("signals", "units") + tuple(err["loc"])
                code = "config.unknown_key" if err["type"] == "extra_forbidden" else "config.bad_type"
                out.append(_issue(loaded, code, f"{dotted(loc)}: {err['msg']}", loc, 1))
        for i, rule in enumerate(_get(units_raw, "rules") or [] if isinstance(units_raw, dict) else []):
            base = ("signals", "units", "rules", i, "match")
            s = _get(rule, "match", "source")
            if isinstance(s, str) and s not in sources:
                out.append(_issue(loaded, "config.bad_reference", f"{dotted(base + ('source',))} names source {s!r}, "
                                  "which is not defined", base + ("source",), 2, suggestion=closest(s, list(sources))))
            for key in ("name_regex", "id_regex"):
                _regex(loaded, _get(rule, "match", key), base + (key,), out)

    # pass 3: semantics
    _secret_literals(loaded, transports, ("transports",), out)
    _secret_literals(loaded, sources, ("sources",), out)
    for tname, t in _items(transports):
        if not isinstance(t, dict):
            continue
        k = t.get("kind")
        base = ("transports", tname)
        need = {"exec_jsonl": ("command",), "mcp_stdio": ("command",), "mcp_http": ("url",), "replay": ("path",)}
        for key in need.get(k, ()):
            if not t.get(key):
                out.append(_issue(loaded, "config.missing_key", f"transport {tname!r} of kind {k} needs {key}", base, 3))
        if k == "codemode":
            if not (t.get("command") or t.get("url")):
                out.append(_issue(loaded, "config.missing_key", f"transport {tname!r} of kind codemode needs "
                                  "command (stdio) or url (streamable HTTP)", base, 3))
            if not t.get("tool"):
                out.append(_issue(loaded, "config.missing_key", f"transport {tname!r} of kind codemode needs the "
                                  "code-execution tool name in tool", base, 3))
    from docket.text.durations import parse_duration
    for sid, src in _items(sources):
        if not isinstance(src, dict):
            continue
        base = ("sources", sid)
        for key, val in _items(src.get("mapping")):
            mp = base + ("mapping", key)
            if key not in MAPPING_KEYS and not str(key).startswith("fields."):
                out.append(_issue(loaded, "config.bad_mapping", f"unknown mapping key {key!r}", mp, 3,
                                  suggestion=closest(key, list(MAPPING_KEYS))))
            if not (isinstance(val, str) and val.startswith("$.") and len(val) > 2):
                out.append(_issue(loaded, "config.bad_mapping", f"mapping {key!r} must be a path like '$.field'", mp, 3))
        kind = source_kind(src, transports)
        if not src.get("transport"):
            if src.get("kind") == "files" and not src.get("path"):
                out.append(_issue(loaded, "config.missing_key", f"source {sid!r} of kind files needs a path", base, 3))
            elif src.get("kind") != "files":
                out.append(_issue(loaded, "config.missing_key",
                                  f"source {sid!r} needs a transport (a key of transports), or kind files with a path",
                                  base, 3, suggestion=closest("transport", list(src))))
        for pk, pv in _items(src.get("params")):
            if pv is None:
                out.append(_issue(loaded, "config.missing_key", f"source {sid!r} must set params.{pk}"
                                  + (f" (the preset {src.get('extends')} leaves it to the workspace)"
                                     if src.get("extends") else ""), base + ("params", pk), 3))
        cur = src.get("cursor") if isinstance(src.get("cursor"), dict) else {}
        model = cur.get("model", "snapshot")
        eff = (cur.get("inner") or "incremental") if model == "cadence" else model
        if model == "cadence" and not (cur.get("weekdays") or cur.get("every_days")):
            out.append(_issue(loaded, "config.missing_key", f"source {sid!r}: the cadence model needs weekdays or "
                              "every_days", base + ("cursor",), 3))
        if isinstance(cur.get("lookback"), str):
            try:
                parse_duration(cur["lookback"])
            except Exception:
                out.append(_issue(loaded, "config.bad_type", f"sources.{sid}.cursor.lookback: not a duration",
                                  base + ("cursor", "lookback"), 3, expected="a duration like 3d or 12h"))
        req = src.get("request") if isinstance(src.get("request"), dict) else {}
        wants = {"units": ("discover", "read"), "id-scan": ("ids", "get")}.get(eff, ("read",))
        for w in wants:
            if not req.get(w):
                out.append(_issue(loaded, "config.missing_key", f"source {sid!r} with cursor model {eff} needs "
                                  f"request.{w}", base + ("request",), 3))
        pg = src.get("paging") if isinstance(src.get("paging"), dict) else {}
        for pth, rx_list in (("paging", pg.get("too_large")), ("resolve", _get(src, "resolve", "patterns"))):
            for i, rx in enumerate(rx_list or [] if isinstance(rx_list, list) else []):
                _regex(loaded, rx, base + (pth, "too_large" if pth == "paging" else "patterns", i), out)
        sizes = [x for x in (pg.get("page_size"), pg.get("min_page_size")) if isinstance(x, int)]
        if len(sizes) == 2 and not (1 <= sizes[1] <= sizes[0]):
            out.append(_issue(loaded, "config.bad_type", f"sources.{sid}.paging: need 1 <= min_page_size <= page_size",
                              base + ("paging",), 3))
        # pass 4: capabilities
        if isinstance(kind, str) and kind not in SOURCE_KINDS:
            out.append(_issue(loaded, "config.unknown_kind", f"unknown source kind {kind!r}", base + ("kind",), 4,
                              suggestion=closest(kind, list(SOURCE_KINDS))))
            continue
        if kind is None:
            continue
        caps = set(src.get("capabilities") or [])
        if eff == "units":
            caps.add("discover")
        if src.get("resolve"):
            caps.add("resolve")
        if src.get("target") == "context":
            caps.add("context")
        for c in sorted(caps - SOURCE_KINDS[kind]):
            out.append(_issue(loaded, "config.capability_missing", f"source {sid!r} needs {c}, which a {kind} "
                              "transport does not provide", base, 4))
        if "resolve" in caps and not req.get("resolve"):
            out.append(_issue(loaded, "config.capability_missing", f"source {sid!r} declares resolve but has no "
                              "request.resolve", base, 4))
        need = _TEMPLATE_NEEDS.get(kind)
        if need:
            for name, tpl in _items(req):
                if isinstance(tpl, dict) and not tpl.get(need[0]):
                    out.append(_issue(loaded, "config.capability_missing",
                                      f"source {sid!r}: request.{name} needs {need[1]} for a {kind} transport",
                                      base + ("request", name), 4))
    return out
