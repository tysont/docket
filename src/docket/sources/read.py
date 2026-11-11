"""A read node's work: read one source, keep what it returned, hand on its items."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from docket.engine.clock import run_date
from docket.sources import records
from docket.sources.reader import Starts, read_sources
from docket.sources.cursors import durable, processed_ids
from docket.transports.base import Secrets

if TYPE_CHECKING:
    from docket.engine.context import RunContext


def read_source(rc: "RunContext", sid: str, schedule: Any = None) -> dict[str, Any]:
    """Read source ``sid`` from its cursor and return the node's output."""
    db = rc.run.store.db
    starts = Starts(durable=durable(db, [sid]), processed={sid: processed_ids(db, sid)})
    clock = rc.run.header.clock
    results = asyncio.run(read_sources(rc.ws, [sid], env=dict(rc.env), instant=clock.instant, run_date=run_date(clock),
                                       starts=starts, log_dir=rc.run.dir / "logs", secrets=Secrets(),
                                       schedule={sid: schedule} if schedule is not None else {}))
    records.persist(rc, sid, results[sid])
    return records.output(rc, sid)
