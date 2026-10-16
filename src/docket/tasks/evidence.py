"""Verbatim evidence against raw records (TD §9.4 step 3).

A quote must appear in the decoded text of the item's raw record after
whitespace normalization only. The record is the entry of its page body (the
blob) at the source's ``paging.entries`` path; decoding collects every string
in it, and a string that is itself JSON (a tool result wrapping a payload) is
decoded in turn. When the entry cannot be located, the whole page is read.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from docket.engine.context import RunContext


def normalize(text: str) -> str:
    return " ".join(str(text).split())


def strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        out.append(value)
        s = value.strip()
        if s[:1] in "{[" and s[-1:] in "}]":
            try:
                strings(json.loads(s), out, depth + 1)
            except ValueError:
                pass
    elif isinstance(value, dict):
        for v in value.values():
            strings(v, out, depth)
    elif isinstance(value, list):
        for v in value:
            strings(v, out, depth)


def decode_entry(blob: bytes, entries_path: str | None, entry: int | None) -> str:
    try:
        body = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return normalize(blob.decode("utf-8", "replace"))
    target: Any = body
    if entries_path and entry is not None:
        from docket.sources.contracts import lookup
        rows = lookup(body, entries_path)
        if isinstance(rows, list) and 0 <= entry < len(rows):
            target = rows[entry]
    out: list[str] = []
    strings(target, out)
    return normalize("\n".join(out))


class RecordTexts:
    """Decoded raw text per record id for one run, read lazily from the record indexes.

    A record read again in a later generation is that generation's version only,
    as in the collect artifact (later generations win).
    """

    def __init__(self, rc: "RunContext", sources: set[str] | None = None) -> None:
        self.rc = rc
        self.sources = sources
        self._index: dict[str, list[dict[str, Any]]] | None = None
        self._cache: dict[str, str] = {}

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._index is None:
            idx: dict[str, tuple[int, list[dict[str, Any]]]] = {}
            for p in self.rc.run.dir.glob("gen-*/records/*.jsonl"):
                if self.sources is not None and p.stem not in self.sources:
                    continue
                try:
                    gen = int(p.parent.parent.name.split("-", 1)[1])
                except ValueError:
                    continue
                for ln in p.read_text(encoding="utf-8").splitlines():
                    if '"record_id"' not in ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except ValueError:
                        continue
                    rid = row.get("record_id")
                    if not rid or not row.get("blob") or row.get("outcome") == "tombstone":
                        continue
                    cur = idx.get(rid)
                    if cur is None or gen > cur[0]:
                        idx[rid] = (gen, [row])
                    elif gen == cur[0]:
                        cur[1].append(row)
            self._index = {k: v[1] for k, v in idx.items()}
        return self._index

    def text(self, record_id: str) -> str | None:
        if record_id in self._cache:
            return self._cache[record_id]
        rows = self._load().get(record_id)
        if not rows:
            return None
        parts = []
        blobs = self.rc.run.store.blobs
        for row in rows:
            if not blobs.exists(row["blob"]):
                continue
            src = self.rc.ws.config.sources.get(row.get("source", ""))
            path = src.paging.entries if src is not None and src.paging is not None else None
            parts.append(decode_entry(blobs.get(row["blob"]), path, row.get("entry")))
        if not parts:
            return None
        text = "\n".join(parts)
        self._cache[record_id] = text
        return text

    def for_records(self, record_ids: list[str]) -> str | None:
        texts = [t for t in (self.text(r) for r in record_ids if r) if t is not None]
        return "\n".join(texts) if texts else None


def record_ids(it: dict[str, Any]) -> list[str]:
    """The raw records an item was mapped from (several once merged)."""
    out: list[str] = []
    for key in ("record_ids", "records"):
        v = it.get(key)
        if isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
            out += [x["record_id"] for x in v if isinstance(x, dict) and isinstance(x.get("record_id"), str)]
    if isinstance(it.get("record_id"), str):
        out.append(it["record_id"])
    return sorted(set(out))
