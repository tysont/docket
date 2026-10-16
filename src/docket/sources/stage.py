"""The collect stage's work: read a generation's sources, persist it, build the artifact."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from docket.engine.clock import run_date
from docket.sources.collect import SourceResult, Starts, read_sources
from docket.sources.cursors import durable, processed_ids, proposals
from docket.sources.generations import (
    build_output, current_generation, generation_spec, open_generation, persist,
)
from docket.transports.base import Secrets

if TYPE_CHECKING:
    from docket.engine.context import RunContext


def read(rc: "RunContext", sids: list[str], *, refresh: bool) -> dict[str, SourceResult]:
    db = rc.run.store.db
    starts = Starts(durable=durable(db, sids), run=proposals(db, rc.run.id, sids),
                    processed={s: processed_ids(db, s) for s in sids}, refresh=refresh)
    clock = rc.run.header.clock
    return asyncio.run(read_sources(rc.ws, sids, env=dict(rc.env), instant=clock.instant, run_date=run_date(clock),
                                    starts=starts, log_dir=rc.run.dir / "logs", secrets=Secrets()))


def execute_collect(rc: "RunContext") -> dict[str, Any]:
    """Read the current generation (opening generation 0 on first use) and return the artifact data."""
    n = current_generation(rc)
    spec = generation_spec(rc, n)
    if spec is None:
        open_generation(rc, n, rc.source_ids(), refresh=False)
        spec = generation_spec(rc, n)
    assert spec is not None
    sids = [s for s in spec["sources"] if s in rc.ws.config.sources]
    results = read(rc, sids, refresh=bool(spec["refresh"]))
    persist(rc, n, results)
    return build_output(rc)


def refresh_sources(rc: "RunContext", requested: list[str] | None) -> list[str]:
    """Sources a refresh reads: those named, else the workflow's refresh_on_resume, else its volatile sources."""
    if requested:
        return list(dict.fromkeys(requested))
    if rc.workflow.refresh_on_resume:
        return list(rc.workflow.refresh_on_resume)
    return [s for s in rc.source_ids() if s in rc.ws.config.sources and rc.ws.config.sources[s].volatile]


def open_refresh(rc: "RunContext", sids: list[str]) -> int:
    n = current_generation(rc) + 1
    open_generation(rc, n, sids, refresh=True)
    rc.invalidate()
    return n
