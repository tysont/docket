"""Generic ledgers (TD §4.2, §13.5): append-only rows ``(ledger, run, key, json)``.

Configured ledgers (``journal.ledgers``) draw one row per element of a journal
context list that matches every ``where`` pair; ``key`` and each field are
templates over that element. Rows are appended in close's state transaction,
once per run, and the journal context carries each ledger's rows from the
most recent runs so a template can aggregate across them (for example a
surfaced-items audit or a carries ledger).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from docket.text import templates


def append(db: sqlite3.Connection, ledger: str, run_id: str | None, key: str | None, row: dict[str, Any]) -> None:
    """Append one row (the caller holds the transaction)."""
    db.execute("INSERT INTO ledgers (ledger, run_id, key, json) VALUES (?,?,?,?)",
               (ledger, run_id, key, json.dumps(row, sort_keys=True, ensure_ascii=False)))


def rows(db: sqlite3.Connection, ledger: str, *, keep_runs: int | None = None) -> list[dict[str, Any]]:
    """Rows of a ledger in append order, each ``{run, key, **fields}``; ``keep_runs`` limits to the newest runs."""
    out: list[dict[str, Any]] = []
    for r in db.execute("SELECT run_id, key, json FROM ledgers WHERE ledger=? ORDER BY id", (ledger,)):
        try:
            body = json.loads(r["json"])
        except ValueError:
            continue
        out.append({**(body if isinstance(body, dict) else {"value": body}), "run": r["run_id"], "key": r["key"]})
    if keep_runs is not None:
        runs: list[str] = []
        for x in out:
            if x["run"] not in runs:
                runs.append(x["run"])
        keep = set(runs[-keep_runs:])
        out = [x for x in out if x["run"] in keep]
    return out


def _matches(el: dict[str, Any], where: dict[str, str]) -> bool:
    for k, v in where.items():
        cur: Any = el
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if str(cur if cur is not None else "") != v:
            return False
    return True


def compute(specs: dict[str, Any], ctx: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(ledger, key, row)`` for every configured ledger over the journal context."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for name in sorted(specs):
        spec = specs[name]
        key_tpl = templates.parse(spec.key)
        field_tpls = {k: templates.parse(v) for k, v in sorted(spec.fields.items())}
        elements: Iterable[Any] = ctx.get(spec.from_) or []
        seen: set[str] = set()
        for el in elements:
            if not isinstance(el, dict) or not _matches(el, spec.where):
                continue
            scope = {**ctx, **el}
            key = templates.render(key_tpl, scope)
            if key in seen:
                continue
            seen.add(key)
            out.append((name, key, {k: templates.render(t, scope) for k, t in field_tpls.items()}))
    return out


def context(db: sqlite3.Connection, specs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {name: rows(db, name, keep_runs=specs[name].keep_runs) for name in sorted(specs)}
