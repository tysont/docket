"""Destinations (TD §11): the adapter registry and the fixed execution order.

``build(dest_id, ws, denv)`` returns the live adapter for a configured
destination; ``.sandbox()`` on it returns its twin. Adapters import lazily so
read-only commands stay fast (TD §15.6).
"""
from __future__ import annotations

import importlib
from typing import Any

from docket.destinations.base import (  # noqa: F401  (the public interface)
    ACTION_KINDS, VERBS, Acceptance, Action, BaseDestination, DestEnv, DestState, Destination, Operation, Receipt,
    VerifyResult, make_op, op_id,
)

ADAPTERS: dict[str, str] = {
    "markdown_tasks": "docket.destinations.markdown:MarkdownTasks",
    "markdown_agenda": "docket.destinations.markdown:MarkdownAgenda",
    "journal": "docket.destinations.markdown:Journal",
    "calendar_file": "docket.destinations.calendar_file:CalendarFile",
    "calendar_exec": "docket.destinations.calendar_exec:CalendarExec",
    "calendar_mcp": "docket.destinations.mcp:CalendarMcp",
    "message_mcp": "docket.destinations.mcp:MessageMcp",
}

# TD §13.3: task store, agendas, calendar, messages, journal.
ORDER: dict[str, int] = {
    "markdown_tasks": 0, "markdown_agenda": 1, "calendar_exec": 2, "calendar_file": 2, "calendar_mcp": 2,
    "message_mcp": 3, "journal": 4,
}


def adapter_class(adapter: str) -> type:
    mod, _, name = ADAPTERS[adapter].partition(":")
    return getattr(importlib.import_module(mod), name)


def build(dest_id: str, cfg: Any, denv: DestEnv, **kw: Any) -> Any:
    return adapter_class(cfg.adapter)(dest_id, cfg, denv, **kw)


def env_for(ws: Any, *, run: Any = None, env: Any = None, call_tool: Any = None) -> DestEnv:
    """The adapter environment for a workspace, and for a run when one is given."""
    date = run.header.clock.date if run is not None else None
    window = None
    if date is not None:
        from datetime import date as _date, timedelta
        d = _date.fromisoformat(date)
        horizon = 42
        window = ((d - timedelta(days=1)).isoformat() + "T00:00:00Z",
                  (d + timedelta(days=horizon)).isoformat() + "T00:00:00Z")
    return DestEnv(
        root=ws.root, profile=ws.config.profile, date=date,
        sandbox_dir=run.paths.sandbox if run is not None else None,
        log_dir=(run.dir / "logs") if run is not None else None,
        env=dict(env or {}), call_tool=call_tool, window=window)


def snapshot_digests(rc: Any, dest_ids: list[str]) -> dict[str, str]:
    """``DestState`` digests for the destinations a plan touches (the input of freshness, TD §13.2)."""
    out = {}
    for did in sorted(set(dest_ids)):
        d = for_run(rc, did)
        try:
            out[did] = d.snapshot().digest
        finally:
            d.close()
    return out


def for_run(rc: Any, dest_id: str) -> Any:
    """The destination a run writes: the twin in a sandbox run, the live adapter otherwise."""
    cfg = rc.ws.config.destinations[dest_id]
    live = build(dest_id, cfg, env_for(rc.ws, run=rc.run, env=rc.env))
    return live.sandbox() if rc.run.header.sandbox else live
