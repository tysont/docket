"""Concurrent collection with isolation (TD §6.6).

``read_sources`` runs every requested source concurrently under a global limit,
a per-transport limit and a per-source unit limit. Each source ends with one
outcome; an exception inside one is caught at the source boundary, recorded
with its raw message, and never cancels another. Transports are built once per
collect and shared by the sources that name them, then closed.

Unexpected shapes are recorded per source and re-emitted in source order once
every source has finished, so the run's records do not depend on scheduling.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from docket.config.source_models import Source as SourceConfig
from docket.shapes.unexpected import Shape, collecting, replay
from docket.sources.base import ALL_UNITS, Cursor, Outcome, Page, Unit
from docket.sources.cursors import cadence_skip, interval_from
from docket.sources.declarative import DeclarativeSource, Limits
from docket.transports.base import Secrets, raw_message

GLOBAL_CONCURRENCY = 8


@dataclass
class UnitResult:
    unit: Unit
    cursor: Cursor
    outcome: Outcome
    pages: list[Page] = field(default_factory=list)
    proposal: tuple[str | None, str | None, bool] = (None, None, False)


@dataclass
class SourceResult:
    sid: str
    kind: str
    model: str
    outcome: Outcome
    units: list[UnitResult] = field(default_factory=list)
    discovered: list[Unit] = field(default_factory=list)
    shapes: list[Shape] = field(default_factory=list)


@dataclass
class Starts:
    """Where each (source, unit) starts: the durable cursor, or this run's proposal on refresh."""
    durable: dict[tuple[str, str], tuple[str | None, str | None]] = field(default_factory=dict)
    run: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    processed: dict[str, set[str]] = field(default_factory=dict)
    refresh: bool = False

    def cursor(self, sid: str, uid: str, cfg: SourceConfig, instant: str) -> Cursor:
        pos, cov = self.durable.get((sid, uid), (None, None))
        gen = None
        if self.refresh and (sid, uid) in self.run:
            p = self.run[(sid, uid)]
            pos, cov, gen = p["position"], p["coverage_end"] or cov, p["generation"]
        return Cursor(cfg.cursor.effective, pos, cov, interval_from(cfg.cursor, instant, cov), instant, gen)

    def last_read(self, sid: str) -> str | None:
        ends = [v[1] for k, v in self.durable.items() if k[0] == sid and v[1]]
        return max(ends) if ends else None


def aggregate(units: list[UnitResult]) -> Outcome:
    if not units:
        return Outcome("empty")
    st = [u.outcome.status for u in units]
    total = sum(u.outcome.entries for u in units)
    agg = Outcome("read", entries=total, pages=sum(u.outcome.pages for u in units),
                  stepdowns=sum(u.outcome.stepdowns for u in units),
                  tombstones=sum(u.outcome.tombstones for u in units),
                  failed_records=sum(u.outcome.failed_records for u in units))
    if all(s in ("read", "empty") for s in st):
        agg.status = "read" if total else "empty"
    elif all(s == "unavailable" for s in st):
        first = units[0].outcome
        agg.status, agg.reason, agg.cause = "unavailable", first.reason, first.cause
    elif all(s in ("failed", "unavailable") for s in st):
        agg.status, agg.cause = "failed", next(u.outcome.cause for u in units if u.outcome.cause)
    else:
        bad = [u for u in units if u.outcome.status not in ("read", "empty")]
        agg.status = "partial"
        agg.reason = f"{len(bad)} of {len(units)} unit(s) incomplete"
        if len(units) == 1:
            agg.position, agg.cause, agg.reason = bad[0].outcome.position, bad[0].outcome.cause, bad[0].outcome.reason
    return agg


async def _read_one(src: DeclarativeSource, sid: str, cfg: SourceConfig, *, starts: Starts, instant: str,
                    run_date: date, entry_limit: int | None, only_unit: str | None) -> SourceResult:
    model = cfg.cursor.effective
    kind = getattr(src.transport, "kind", "?")
    res = SourceResult(sid, kind, model, Outcome("read"))
    skip = cadence_skip(cfg.cursor, run_date, starts.last_read(sid))
    if skip:
        res.outcome = Outcome("skipped", reason=skip)
        return res
    auth = await src.check_auth()
    if not auth.ok:
        res.outcome = Outcome("unavailable", reason=auth.reason, instructions=auth.instructions,
                              cause={"code": "source.auth_failed", "message": auth.reason})
        return res
    opened = await src.transport.open()
    if opened is not None:
        status = "unavailable" if opened.kind == "unavailable" else "failed"
        res.outcome = Outcome(status, cause=opened.as_dict(), reason=opened.message if status == "unavailable" else None,
                              instructions=cfg.auth.instructions if cfg.auth and status == "unavailable" else None)
        return res
    jobs: list[tuple[Unit, Cursor, dict[str, Any]]] = []
    if model == "id-scan":
        ids = await src.list_ids()
        if isinstance(ids, Outcome):
            res.outcome = ids
            return res
        done = starts.processed.get(sid, set())
        tpl = cfg.request.get if cfg.request else None
        for i in ids:
            if i in done or (only_unit and i != only_unit):
                continue
            jobs.append((Unit(i, i), Cursor("id-scan", None, None, None, instant), {"id": i, "template": tpl}))
    else:
        if model == "units":
            found = await src.discover()
            if isinstance(found, Outcome):
                res.outcome = found
                return res
            res.discovered = found
        else:
            found = [Unit(ALL_UNITS)]
        for u in found:
            if only_unit and u.id != only_unit:
                continue
            jobs.append((u, starts.cursor(sid, u.id, cfg, instant), {}))
    sem = asyncio.Semaphore(max(1, cfg.budget.concurrency))

    async def one(unit: Unit, cur: Cursor, extra: dict[str, Any]) -> UnitResult:
        async with sem:
            tpl = extra.pop("template", None)
            reader = src.read(None, unit, cur, entry_limit=entry_limit, template=tpl, extra=extra)
            pages = [p async for p in reader]
            assert reader.outcome is not None
            return UnitResult(unit, cur, reader.outcome, pages, reader.proposal)

    results = await asyncio.gather(*(one(u, c, e) for u, c, e in jobs), return_exceptions=True)
    for (u, c, _), r in zip(jobs, results, strict=True):
        if isinstance(r, BaseException):
            r = UnitResult(u, c, Outcome("failed", cause={"code": "source.failed", "message": raw_message(r)}))
        res.units.append(r)
    res.units.sort(key=lambda x: x.unit.id)
    res.outcome = aggregate(res.units) if model != "id-scan" or res.units else Outcome("empty")
    return res


def _build_transports(ws: Any, names: set[str], *, env: Mapping[str, str], log_dir: Path | None,
                      secrets: Secrets) -> dict[str, tuple[Any, Any]]:
    from docket.transports.registry import build_transport
    out: dict[str, tuple[Any, Any]] = {}
    for name in sorted(names):
        cfg = ws.config.transports[name]
        out[name] = build_transport(name, cfg, root=ws.root, env=env, log_dir=log_dir, secrets=secrets)
    return out


def implicit_transport() -> Any:
    """A ``files`` source with no transport reads through a files transport rooted at the workspace."""
    from docket.config.source_models import TransportConfig
    return TransportConfig(kind="files")


async def read_sources(ws: Any, sids: list[str], *, env: Mapping[str, str], instant: str, run_date: date,
                       starts: Starts, log_dir: Path | None, secrets: Secrets, entry_limit: int | None = None,
                       only_unit: str | None = None) -> dict[str, SourceResult]:
    from docket.transports.registry import build_transport
    named = {ws.config.sources[s].transport for s in sids if ws.config.sources[s].transport}
    transports = _build_transports(ws, {n for n in named if n}, env=env, log_dir=log_dir, secrets=secrets)
    gsem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    tsems = {n: asyncio.Semaphore(max(1, ws.config.transports[n].concurrency)) for n in transports}
    implicit: dict[str, Any] = {}

    async def guarded(sid: str) -> SourceResult:
        cfg: SourceConfig = ws.config.sources[sid]
        with collecting() as col:
            try:
                if cfg.transport:
                    t, problem = transports[cfg.transport]
                    sem = tsems[cfg.transport]
                else:
                    t, problem = build_transport(f"{sid}-files", implicit_transport(), root=ws.root, env=env,
                                                 log_dir=log_dir, secrets=secrets)
                    implicit[sid] = t
                    sem = asyncio.Semaphore(4)
                kind = ws.config.transports[cfg.transport].kind if cfg.transport else "files"
                if problem is not None:
                    out = SourceResult(sid, kind, cfg.cursor.effective,
                                       Outcome("unavailable", reason=problem.message, cause=problem.as_dict(),
                                               instructions=cfg.auth.instructions if cfg.auth else None))
                else:
                    src = DeclarativeSource(sid, cfg, t, secrets=secrets, limits=Limits(gsem, sem), env=dict(env))
                    out = await _read_one(src, sid, cfg, starts=starts, instant=instant, run_date=run_date,
                                          entry_limit=entry_limit, only_unit=only_unit)
                    out.kind = kind
            except Exception as e:  # the source boundary: isolate, keep the raw message
                out = SourceResult(sid, "?", cfg.cursor.effective,
                                   Outcome("failed", cause={"code": "source.failed",
                                                            "message": secrets.mask(raw_message(e))}))
            out.shapes = sorted(col.drain(), key=lambda s: (s.area, s.detail))
            return out

    try:
        done = await asyncio.gather(*(guarded(s) for s in sids))
    finally:
        for t, _ in transports.values():
            if t is not None:
                await _close(t)
        for t in implicit.values():
            if t is not None:
                await _close(t)
    out = {r.sid: r for r in done}
    for sid in sorted(out):
        for s in out[sid].shapes:
            replay(s)
    return out


async def _close(t: Any) -> None:
    try:
        await t.close()
    except Exception:  # closing never fails a collect; the outcome is already recorded
        pass
