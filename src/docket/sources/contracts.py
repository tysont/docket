"""Response contracts (TD §6.7).

A contract declares, per response kind, the fields read and their types, which
are optional, enumerations, and the safe reading for each variation. The
checker runs on every entry of every page:

- a missing optional field is not drift (recorded only when its variation says so);
- a missing required field fails that record only, records an unexpected shape,
  and the unit continues;
- a type change on a required field fails the unit (``source.contract.type_changed``)
  with the sample; other units continue;
- an unknown enumeration value takes the variation's reading (``value`` or ``use``),
  or keeps the value as received, and is recorded;
- a tombstone (the contract's ``tombstone`` path is truthy) is read as deleted.

The checker never raises on data. The raw entry is untouched; ``view`` is the
entry with readings applied, which mapping reads.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from docket.config.source_models import ContractKind, VariationRule
from docket.shapes.unexpected import unexpected
from docket.transports.base import Secrets

_MISSING = object()
Verdict = Literal["ok", "record_failed", "type_changed", "tombstone"]


def get_path(value: Any, path: str) -> Any:
    """``$.a.b`` or ``a.b``; ``$`` is the value itself. Missing yields ``_MISSING``."""
    p = path[2:] if path.startswith("$.") else ("" if path == "$" else path)
    if not p:
        return value
    cur = value
    for part in p.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def lookup(value: Any, path: str | None) -> Any:
    """``get_path`` with ``None`` for missing."""
    if not path:
        return None
    v = get_path(value, path)
    return None if v is _MISSING else v


def set_path(value: dict[str, Any], path: str, new: Any) -> None:
    parts = (path[2:] if path.startswith("$.") else path).split(".")
    cur = value
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = new


def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _is_timestamp(v: Any) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if not isinstance(v, str) or len(v) < 10:
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def type_ok(v: Any, typ: str) -> bool:
    if typ == "any":
        return True
    if typ == "str":
        return isinstance(v, str)
    if typ == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if typ in ("float", "number"):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if typ == "bool":
        return isinstance(v, bool)
    if typ == "timestamp":
        return _is_timestamp(v)
    if typ == "list":
        return isinstance(v, list)
    if typ == "dict":
        return isinstance(v, dict)
    return True


@dataclass
class Checked:
    verdict: Verdict
    view: dict[str, Any] | None
    detail: str | None = None
    sample: str | None = None


class ContractChecker:
    def __init__(self, source_id: str, contract: dict[str, ContractKind], *, extend_base: str,
                 secrets: Secrets | None = None) -> None:
        self.sid = source_id
        self.contract = contract
        self.extend_base = extend_base
        self.secrets = secrets or Secrets()

    def _sample(self, raw: Any) -> str:
        text = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
        return self.secrets.mask(text)[:500]

    def _extend(self, kind: str, section: str, field: str, rule: VariationRule | None = None) -> str:
        if rule is not None and rule.extend:
            return rule.extend
        return f"{self.extend_base}.{kind}.{section}.{field}"

    def _apply(self, view: dict[str, Any], raw: Any, field: str, rule: VariationRule) -> Any:
        """Take a variation's reading; returns the value now at ``field`` (or ``_MISSING``)."""
        if rule.use:
            alt = get_path(raw, rule.use)
            if alt is not _MISSING:
                set_path(view, field, alt)
                return alt
        if rule.value is not None:
            set_path(view, field, rule.value)
            return rule.value
        return _MISSING

    def check(self, kind: str, raw: Any) -> Checked:
        sid = self.sid
        if not isinstance(raw, dict):
            unexpected("source.contract", f"{sid}.{kind} entry is not an object", sample=self._sample(raw),
                       reading="skip the record", extend=f"{self.extend_base}.{kind}")
            return Checked("record_failed", None, "not an object")
        ck = self.contract.get(kind)
        if ck is None:
            return Checked("ok", raw)
        if ck.tombstone and get_path(raw, ck.tombstone) not in (_MISSING, None, False, "", 0):
            return Checked("tombstone", None)
        view = copy.deepcopy(raw)
        for field, typ in ck.required.items():
            var = ck.variations.get(field)
            v = get_path(raw, field)
            if v is _MISSING or v is None:
                rule = var.missing if var else None
                got = self._apply(view, raw, field, rule) if rule else _MISSING
                if rule and rule.record:
                    unexpected("source.contract", f"{sid}.{kind}.{field} missing", sample=self._sample(raw),
                               reading=rule.reading, extend=f"{self._extend(kind, "required", field, rule)}")
                if got is _MISSING:
                    if not (rule and rule.record):
                        unexpected("source.contract", f"{sid}.{kind}.{field} missing", sample=self._sample(raw),
                                   reading="skip the record; the unit continues",
                                   extend=f"{self._extend(kind, "required", field)}")
                    return Checked("record_failed", None, f"{field} missing")
                continue
            if not type_ok(v, typ):
                rule = var.type if var else None
                if rule is not None:
                    self._apply(view, raw, field, rule)
                    if rule.record:
                        unexpected("source.contract", f"{sid}.{kind}.{field} type {typ}->{type_name(v)}",
                                   sample=self._sample(raw), reading=rule.reading,
                                   extend=f"{self._extend(kind, "required", field, rule)}")
                    continue
                detail = f"{sid}.{kind}.{field} type {typ}->{type_name(v)}"
                sample = self._sample(raw)
                unexpected("source.contract", detail, sample=sample,
                           reading="fail this unit; other units continue",
                           extend=f"{self._extend(kind, "required", field)}")
                return Checked("type_changed", None, detail, sample)
        for field, typ in ck.optional.items():
            var = ck.variations.get(field)
            v = get_path(raw, field)
            if v is _MISSING or v is None:
                rule = var.missing if var else None
                if rule is not None:
                    self._apply(view, raw, field, rule)
                    if rule.record:
                        unexpected("source.contract", f"{sid}.{kind}.{field} missing", sample=self._sample(raw),
                                   reading=rule.reading, extend=f"{self._extend(kind, "optional", field, rule)}")
                continue
            if not type_ok(v, typ):
                rule = var.type if var else None
                if rule is not None:
                    got = self._apply(view, raw, field, rule)
                    if got is _MISSING:
                        set_path(view, field, None)
                reading = rule.reading if rule else "read the field as absent"
                if rule is None:
                    set_path(view, field, None)
                if rule is None or rule.record:
                    unexpected("source.contract", f"{sid}.{kind}.{field} type {typ}->{type_name(v)}",
                               sample=self._sample(raw), reading=reading,
                               extend=f"{self._extend(kind, "optional", field, rule)}")
        for field, allowed in ck.enums.items():
            v = get_path(view, field)
            if v is _MISSING or v is None or v in allowed:
                continue
            var = ck.variations.get(field)
            rule = var.unknown if var else None
            if rule is not None:
                self._apply(view, raw, field, rule)
            if rule is None or rule.record:
                unexpected("source.contract", f"{sid}.{kind}.{field}={v}", sample=self._sample(raw),
                           reading=rule.reading if rule else "keep the value as received",
                           extend=f"{self._extend(kind, "enums", field, rule)}")
        if ck.unknown_fields == "record":
            known = {f.split(".")[0].removeprefix("$.") for f in (*ck.required, *ck.optional, *ck.enums)}
            if ck.tombstone:
                known.add(ck.tombstone.removeprefix("$.").split(".")[0])
            for key in sorted(set(raw) - known):
                unexpected("source.contract", f"{sid}.{kind} field {key} not in contract", sample=self._sample(raw),
                           reading="ignore the field", extend=f"{self.extend_base}.{kind}.optional")
        return Checked("ok", view)
