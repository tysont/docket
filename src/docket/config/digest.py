"""Canonical JSON and digests (TD §3.5, §4.4).

Canonical JSON: sorted keys, no insignificant whitespace, UTF-8, integral
floats written as integers, NaN and infinity refused.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalize(v: Any) -> Any:
    if isinstance(v, bool) or v is None or isinstance(v, (str, int)):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else v
    if isinstance(v, dict):
        return {str(k): _normalize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_normalize(x) for x in v]
    if hasattr(v, "model_dump"):
        return _normalize(v.model_dump(mode="json"))
    if hasattr(v, "isoformat"):
        return v.isoformat()
    raise TypeError(f"not canonicalizable: {type(v).__name__}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """SHA-256 of the canonical JSON of ``value``, as 64 hex characters."""
    return sha256_hex(canonical_json(value))


def pretty_json(value: Any) -> str:
    """Deterministic human-readable JSON for files the agent reads."""
    return json.dumps(_normalize(value), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def config_digests(resolved: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Return the configuration digest and one partial digest per top-level section.

    ``includes`` is excluded: how the workspace splits its files is not configuration.
    """
    body = {k: v for k, v in resolved.items() if k != "includes"}
    return digest(body), {k: digest(v) for k, v in sorted(body.items())}
