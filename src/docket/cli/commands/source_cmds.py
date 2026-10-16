"""Collection commands (M2): ``collect [--refresh] [--sources]``, ``source list``,
``source test <id> [--unit] [--limit]`` and ``units list``."""
from __future__ import annotations

from typing import Any

from docket.cli.commands import stage_cmds
from docket.cli.commands.run_cmds import _guard_mutable, _step_next, step_summary
from docket.cli.envelope import Result
from docket.cli.errors import ExternalError, InputError, StateError
from docket.cli.registry import Arg, command


def _split_ids(text: str | None) -> list[str]:
    return [s.strip() for s in (text or "").split(",") if s.strip()]


def _check_ids(rc: Any, ids: list[str]) -> None:
    from docket.config.loader import closest
    known = [s for s in rc.source_ids() if s in rc.ws.config.sources]
    for s in ids:
        if s not in known:
            raise InputError("input.unknown_source", f"Workflow {rc.workflow_name()!r} reads no source {s!r}.",
                             hint="docket source list", details={"suggestion": closest(s, known), "sources": known})


def collect_command(ctx: Any, ns: Any) -> Result:
    """``docket collect``: the collect stage, or a refresh generation (TD §4.5)."""
    from docket.sources.stage import open_refresh, refresh_sources
    rc = ctx.run_context()
    requested = _split_ids(getattr(ns, "sources", None))
    if requested and not ns.refresh:
        raise InputError("input.bad_argument", "--sources selects the sources of a refresh; add --refresh.",
                         hint=" ".join(rc.argv("collect", "--refresh", "--sources", ",".join(requested))))
    ev = rc.eval("collect")
    waiting = ev.reason == "sources_unavailable"
    # A first collect, or a stale one (configuration changed, or a refresh interrupted before
    # its artifact), runs the current generation; a refresh opens the next one.
    if (not ns.refresh and not waiting) or ev.state in ("pending", "stale"):
        return stage_cmds.advance(ctx, "collect")
    _guard_mutable(rc)
    if rc.run.header.applied_plan:
        raise StateError("stage.post_apply_change", "Apply wrote receipts; a refresh cannot change this run's inputs.",
                         hint="docket run start <workflow> --new")
    if requested:
        _check_ids(rc, requested)
    sids = refresh_sources(rc, requested) if requested or ns.refresh else []
    if waiting and not requested:
        sids = sorted(set(sids) | set(ev.extra["unavailable"]))
    if not sids:
        step = stage_cmds._compute(ctx, rc)
        return Result({"refresh": True, "sources": [], "changed": False, "step": step.model_dump()},
                      f"no volatile sources to refresh\n{step_summary(step)}", next=_step_next(step))
    if ctx.dry_run:
        return Result({"dry_run": True, "refresh": True, "would_read": sids},
                      f"would open a new generation reading {', '.join(sids)}")
    ctx.acquire(rc)
    rc.materialize()
    n = open_refresh(rc, sids)
    ev = rc.eval("collect")
    from docket.engine.stages import STAGE_IMPLS
    impl = STAGE_IMPLS["collect"]
    data = impl.execute(rc, ev, None)
    impl.finish(rc, ev, data)
    rc.invalidate()
    issued = stage_cmds._auto_issue(rc)
    step = stage_cmds._compute(ctx, rc)
    return Result({"stage": "collect", "status": "current", "changed": True, "digest": ev.digest, "refresh": True,
                   "generation": n, "sources": sids, "result": stage_cmds._brief(data), "issued_tasks": issued,
                   "step": step.model_dump()},
                  f"refreshed {', '.join(sids)} as generation {n}\n{step_summary(step)}", next=_step_next(step))


@command("source list", "List configured sources: transport, cursor model, capabilities and workflows.",
         examples=["docket source list --json"])
def cmd_source_list(ctx: Any, ns: Any) -> Result:
    ws = ctx.workspace()
    rows = []
    for sid in sorted(ws.config.sources):
        s = ws.config.sources[sid]
        kind = ws.config.transports[s.transport].kind if s.transport else "files"
        caps = set(s.capabilities or ["read"])
        if s.cursor.effective == "units":
            caps.add("discover")
        if s.resolve:
            caps.add("resolve")
        if s.target == "context":
            caps.add("context")
        rows.append({"id": sid, "kind": kind, "transport": s.transport, "extends": s.extends,
                     "cursor": s.cursor.model, "capabilities": sorted(caps), "target": s.target,
                     "volatile": s.volatile, "required": s.required, "description": s.description,
                     "workflows": sorted(w for w, wf in ws.config.workflows.items()
                                         if wf.sources is None or sid in wf.sources)})
    text = "\n".join(f"{r['id']:<16} {r['kind']:<11} {r['cursor']:<12} {','.join(r['capabilities'])}" for r in rows)
    return Result({"sources": rows}, text or "no sources")


@command("source test", "Read a small sample from one source and show the mapped items; writes nothing durable.",
         args=[Arg("id", "Source id."), Arg("--unit", "Read only this unit."),
               Arg("--limit", "Entries to read (default 5).", default="5"),
               Arg("--now", "Read as of this ISO instant instead of the wall clock (recorded transports, tests).")],
         effects=["contacts the source's system; writes no record, cursor, unit row or run file"],
         examples=["docket source test chat --limit 3 --json", "docket source test chat --unit spaces/room-1 --json"])
def cmd_source_test(ctx: Any, ns: Any) -> Result:
    import asyncio

    from docket.config.loader import closest
    from docket.sources.collect import Starts, read_sources
    from docket.sources.generations import map_entry
    from docket.transports.base import Secrets
    ws = ctx.workspace()
    if ns.id not in ws.config.sources:
        raise InputError("input.unknown_source", f"No source {ns.id!r}.", hint="docket source list",
                         details={"suggestion": closest(ns.id, list(ws.config.sources)),
                                  "sources": sorted(ws.config.sources)})
    try:
        limit = max(1, int(ns.limit))
    except ValueError:
        raise InputError("input.bad_argument", f"--limit must be a number, not {ns.limit!r}") from None
    from docket.engine.clock import pin, run_date
    clock = pin(ws.config.profile.timezone, now=ns.now)
    cfg = ws.config.sources[ns.id]
    durable: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    store = ctx.store()
    if (store.root / "docket.db").is_file():  # never create state: a sample reads from the source's start
        from docket.sources.cursors import durable as durable_rows
        durable = durable_rows(store.db, [ns.id])
    res = asyncio.run(read_sources(ws, [ns.id], env=dict(ctx.env), instant=clock.instant, run_date=run_date(clock),
                                   starts=Starts(durable=durable), log_dir=None, secrets=Secrets(),
                                   entry_limit=limit, only_unit=ns.unit))[ns.id]
    if cfg.cursor.effective == "units" and ns.unit and not res.units and res.outcome.status not in (
            "failed", "unavailable", "skipped"):
        raise InputError("input.unknown_unit", f"Source {ns.id!r} discovered no unit {ns.unit!r}.",
                         hint=f"docket source test {ns.id}",
                         details={"units": [u.id for u in res.discovered][:50]})
    if res.outcome.status in ("failed", "unavailable") and not any(u.pages for u in res.units):
        cause = res.outcome.cause or {}
        code = cause.get("code") if cause.get("code") in _EXTERNAL else (
            "source.auth_failed" if res.outcome.status == "unavailable" and cfg.auth else "source.unavailable")
        raise ExternalError(code, res.outcome.reason or cause.get("message") or res.outcome.status,
                            hint=res.outcome.instructions or f"docket source test {ns.id}",
                            details={"outcome": res.outcome.as_dict()})
    items, units = [], []
    for ur in res.units:
        units.append({"unit": ur.unit.id, "name": ur.unit.name, "outcome": ur.outcome.as_dict()})
        for page in ur.pages:
            for e in page.entries:
                if e.view is not None and len(items) < limit:
                    items.append(map_entry(ns.id, ur.unit.id, e.view, cfg.mapping, f"sample#{page.number}.{e.index}"))
    data = {"source": ns.id, "outcome": res.outcome.as_dict(), "units": units,
            "discovered": [{"id": u.id, "name": u.name, "type": u.type, "members": u.members} for u in res.discovered],
            "items": items, "written": False}
    text = [f"{ns.id}: {res.outcome.status} ({len(items)} item(s) shown)"]
    text += [f"  [{it['unit'] or '-'}] {it['title']}" for it in items]
    return Result(data, "\n".join(text))


_EXTERNAL = {"source.failed", "source.auth_failed", "source.contract.type_changed", "source.contract.not_json",
             "source.budget_exhausted", "source.truncated", "transport.timeout", "transport.too_large",
             "transport.status", "transport.protocol", "transport.unavailable", "transport.no_recording",
             "transport.error"}


@command("units list", "List the unit registry: discovered units and their classification.",
         args=[Arg("--source", "Only units of this source.")], examples=["docket units list --json"])
def cmd_units_list(ctx: Any, ns: Any) -> Result:
    ctx.workspace_root()
    db = ctx.store().db
    if ns.source:
        rows = db.execute("SELECT * FROM units WHERE source=? ORDER BY source, unit_id", (ns.source,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM units ORDER BY source, unit_id").fetchall()
    out = [dict(r) for r in rows]
    text = "\n".join(f"{r['source']:<12} {r['unit_id']:<28} {r['kind'] or '-':<10} {r['class'] or '-':<12} "
                     f"{r['classified_by'] or 'unclassified'}" for r in out)
    return Result({"units": out}, text or "no units")
