"""``DeclarativeSource``: a source interpreted from configuration (TD §6.2).

The request plan names the requests; the paging contract names where a page
keeps its entries, its continuation and its end-of-data signal; the response
contract checks each entry. Nothing here knows a system.

Coverage is proven from what was read, never from a count the server claims
(TD §6.4). A page with fewer rows than requested ends the unit only when the
paging contract lists ``short_page`` among its end signals; otherwise the next
page is requested.

The pager (TD §6.5) requests ``page_size`` rows. On a truncation signal (a
response over the transport's size cap, the contract's truncation marker, or a
failure the contract classifies as too large) it halves the page size and
retries the same position, down to ``min_page_size``; below that the unit is
``partial`` with its retry position. ``max_pages`` and ``max_seconds`` end
collection as ``partial``, never ``failed``.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from docket.config.source_models import Source as SourceConfig
from docket.shapes.unexpected import unexpected
from docket.sources.base import ALL_UNITS, AuthStatus, Cursor, Entry, Outcome, Page, Resolution, Unit
from docket.sources.contracts import ContractChecker, lookup
from docket.sources.cursors import max_key
from docket.sources.plan import render
from docket.transports.base import Failure, Request, Response, Secrets, child_env, kill_group, raw_message

AUTH_OUTPUT = 1000


@dataclass
class Budget:
    """A source's shared budget: requests made and the deadline."""
    max_pages: int | None
    max_seconds: float | None
    started: float = field(default_factory=time.monotonic)
    pages: int = 0

    def exhausted(self) -> str | None:
        if self.max_pages is not None and self.pages >= self.max_pages:
            return f"budget: max_pages {self.max_pages} reached"
        if self.max_seconds is not None and time.monotonic() - self.started >= self.max_seconds:
            return f"budget: max_seconds {self.max_seconds:g} reached"
        return None


@dataclass
class Limits:
    """Concurrency: one global semaphore and one per transport instance (TD §6.6)."""
    global_sem: asyncio.Semaphore
    transport_sem: asyncio.Semaphore


def _token(v: Any) -> str | None:
    if v is None or v == "" or v is False:
        return None
    return str(v)


def _truthy(v: Any) -> bool:
    return v not in (None, False, "", 0, [], {})


def failure_cause(f: Failure) -> dict[str, Any]:
    return f.as_dict()


class DeclarativeSource:
    def __init__(self, sid: str, cfg: SourceConfig, transport: Any, *, secrets: Secrets, limits: Limits,
                 env: dict[str, str], params: dict[str, Any] | None = None) -> None:
        self.id = sid
        self.cfg = cfg
        self.transport = transport
        self.secrets = secrets
        self.limits = limits
        self.env = env
        caps = set(cfg.capabilities or ["read"])
        if cfg.cursor.effective == "units":
            caps.add("discover")
        if cfg.resolve:
            caps.add("resolve")
        if cfg.target == "context":
            caps.add("context")
        self.capabilities = caps
        base = f"preset {cfg.extends}: contract" if cfg.extends else f"sources.{sid}.contract"
        self.checker = ContractChecker(sid, cfg.contract, extend_base=base, secrets=secrets)
        self.budget = Budget(cfg.budget.max_pages, cfg.budget.max_seconds)
        self.params = dict(cfg.params if params is None else params)

    # --- helpers ---------------------------------------------------------------
    def context(self, unit: Unit | None, cursor: Cursor | None, **extra: Any) -> dict[str, Any]:
        cur = cursor or Cursor(self.cfg.cursor.effective)
        start = cur.position if cur.position is not None else cur.interval_from
        ctx: dict[str, Any] = {
            "unit": ({"id": unit.id, "name": unit.name, "type": unit.type} if unit and unit.id != ALL_UNITS
                     else {"id": None}),
            "cursor": {"position": cur.position, "start": start, "coverage_end": cur.coverage_end},
            "interval": {"from": cur.interval_from, "to": cur.interval_to},
            "params": self.params,
            "source": {"id": self.id, "path": self.cfg.path},
        }
        ctx.update(extra)
        return ctx

    async def send(self, payload: dict[str, Any]) -> Response:
        async with self.limits.global_sem, self.limits.transport_sem:
            return await self.transport.request(Request(payload))

    def _too_large(self, resp: Response, body: Any) -> bool:
        if resp.truncated:
            return True
        if resp.error is not None:
            if resp.error.kind == "too_large":
                return True
            return any(re.search(rx, resp.error.message, re.I) for rx in self.cfg.paging.too_large)
        return bool(self.cfg.paging.truncated and _truthy(lookup(body, self.cfg.paging.truncated)))

    def _parse(self, resp: Response) -> tuple[Any, dict[str, Any] | None]:
        try:
            return json.loads(resp.body.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError):
            text = self.secrets.mask(resp.body.decode("utf-8", "replace"))
            unexpected("source.contract", f"{self.id} response is not JSON", sample=text[:500],
                       reading="fail this unit; other units continue", extend=f"sources.{self.id}.paging")
            return None, {"code": "source.contract.not_json", "message": text[:500]}

    # --- auth --------------------------------------------------------------------
    async def check_auth(self, ctx: Any = None) -> AuthStatus:
        auth = self.cfg.auth
        if auth is None or not auth.check:
            return AuthStatus(True)
        instructions = auth.instructions or f"Fix the credentials for source {self.id}, then rerun the collect command."
        try:
            proc = await asyncio.create_subprocess_exec(
                *auth.check, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=child_env(self.env, {}), start_new_session=True)
        except OSError as e:
            return AuthStatus(False, self.secrets.mask(f"auth check could not start: {raw_message(e)}"), instructions)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), auth.timeout_s)
        except asyncio.TimeoutError:
            kill_group(proc)
            await proc.wait()
            return AuthStatus(False, f"auth check timed out after {auth.timeout_s:g}s (treated as interactive)",
                              instructions)
        if proc.returncode != 0:
            text = (err or out or b"").decode("utf-8", "replace").strip()[-AUTH_OUTPUT:]
            msg = f"auth check exited with status {proc.returncode}" + (f": {text}" if text else "")
            return AuthStatus(False, self.secrets.mask(msg), instructions)
        return AuthStatus(True)

    # --- discover ----------------------------------------------------------------
    async def discover(self, ctx: Any = None) -> list[Unit] | Outcome:
        req = self.cfg.request.discover if self.cfg.request else None
        dc = self.cfg.discover
        if req is None or dc is None:
            return [Unit(ALL_UNITS)]
        units: dict[str, Unit] = {}
        token: str | None = None
        for number in range(1, 101):
            payload = render(req.payload(), self.context(None, None, page={"token": token, "number": number}))
            resp = await self.send(payload)
            self.budget.pages += 1
            if resp.error is not None:
                status = "unavailable" if resp.error.kind == "unavailable" else "failed"
                return Outcome(status, cause=failure_cause(resp.error),
                               reason=resp.error.message if status == "unavailable" else None)
            body, bad = self._parse(resp)
            if bad is not None:
                return Outcome("failed", cause=bad)
            rows = lookup(body, dc.entries)
            if not isinstance(rows, list):
                unexpected("source.contract", f"{self.id} discover response has no {dc.entries} list",
                           reading="discover no units", extend=f"sources.{self.id}.discover.entries")
                rows = []
            for raw in rows:
                checked = self.checker.check(dc.kind, raw)
                if checked.view is None:
                    continue
                uid = lookup(checked.view, dc.id)
                if uid in (None, ""):
                    unexpected("source.unit", f"{self.id} unit without id", sample=self.checker._sample(raw),
                               reading="skip the unit", extend=f"sources.{self.id}.discover.id")
                    continue
                name = lookup(checked.view, dc.name) if dc.name else None
                if name in (None, ""):
                    unexpected("source.unit", f"{self.id} unit without name", sample=self.checker._sample(raw),
                               reading="use the unit id as its name", extend=f"sources.{self.id}.discover.name")
                    name = str(uid)
                utype = lookup(checked.view, dc.type) if dc.type else None
                members = lookup(checked.view, dc.members) if dc.members else None
                units[str(uid)] = Unit(str(uid), str(name), None if utype is None else str(utype),
                                       members if isinstance(members, int) and not isinstance(members, bool) else None,
                                       checked.view)
            token = _token(lookup(body, dc.next)) if dc.next else None
            if token is None:
                break
        return [units[k] for k in sorted(units)]

    async def list_ids(self) -> list[str] | Outcome:
        """id-scan: list ids with ``request.ids``; ids sit at ``discover.entries``/``discover.id``."""
        req = self.cfg.request.ids if self.cfg.request else None
        dc = self.cfg.discover
        if req is None:
            return []
        resp = await self.send(render(req.payload(), self.context(None, None)))
        self.budget.pages += 1
        if resp.error is not None:
            return Outcome("failed", cause=failure_cause(resp.error))
        body, bad = self._parse(resp)
        if bad is not None:
            return Outcome("failed", cause=bad)
        rows = lookup(body, dc.entries if dc else "$.ids")
        out: list[str] = []
        for raw in rows if isinstance(rows, list) else []:
            v = raw if not isinstance(raw, dict) else lookup(raw, dc.id if dc else "$.id")
            if v not in (None, ""):
                out.append(str(v))
        return sorted(set(out))

    # --- read --------------------------------------------------------------------
    def read(self, ctx: Any, unit: Unit | None, cursor: Cursor, *, entry_limit: int | None = None,
             template: Any = None, extra: dict[str, Any] | None = None) -> "UnitReader":
        return UnitReader(self, unit, cursor, entry_limit=entry_limit, template=template, extra=extra or {})

    # --- resolve -----------------------------------------------------------------
    def claims(self, ref: str) -> re.Match[str] | None:
        for rx in (self.cfg.resolve.patterns if self.cfg.resolve else []):
            m = re.search(rx, ref)
            if m:
                return m
        return None

    async def resolve(self, ctx: Any, ref: str) -> Resolution | None:
        m = self.claims(ref)
        req = self.cfg.request.resolve if self.cfg.request else None
        if m is None or req is None or self.cfg.resolve is None:
            return None
        payload = render(req.payload(), self.context(None, None, ref=ref, match=m.groupdict()))
        resp = await self.send(payload)
        if resp.error is not None:
            return Resolution(self.id, ref, status="failed", cause=failure_cause(resp.error))
        body, bad = self._parse(resp)
        if bad is not None:
            return Resolution(self.id, ref, status="failed", cause=bad)
        f = self.cfg.resolve.fields

        def val(k: str) -> str | None:
            v = lookup(body, f.get(k)) if f.get(k) else None
            return None if v is None else str(v)
        return Resolution(self.id, ref, state=val("state"), last_activity=val("last_activity"),
                          last_actor=val("last_actor"), title=val("title"))


class UnitReader:
    """Pages of one unit, read to the end-of-data signal, a budget, or a failure."""

    def __init__(self, src: DeclarativeSource, unit: Unit | None, cursor: Cursor, *, entry_limit: int | None,
                 template: Any, extra: dict[str, Any]) -> None:
        self.src = src
        self.unit = unit
        self.cursor = cursor
        self.entry_limit = entry_limit
        self.template = template
        self.extra = extra
        self.outcome: Outcome | None = None
        self.proposal: tuple[str | None, str | None, bool] = (None, None, False)

    def __aiter__(self) -> AsyncIterator[Page]:
        return self._pages()

    def _finish(self, out: Outcome, position: str | None, last_key: Any) -> None:
        cur = self.cursor
        complete = out.status in ("read", "empty")
        cov = cur.interval_to if complete else (last_key if isinstance(last_key, str) else None)
        if cur.model == "snapshot":
            position = cur.interval_to if complete else None
        moved = position is not None and position != cur.position
        self.proposal = (position if (moved or complete) else None, cov, complete)
        self.outcome = out

    async def _pages(self) -> AsyncIterator[Page]:
        src, cfg = self.src, self.src.cfg
        pg, bd = cfg.paging, cfg.budget
        size = bd.page_size or pg.page_size
        min_size = min(bd.min_page_size or pg.min_page_size, size)
        by_token = cfg.cursor.position == "token"
        token = self.cursor.position if by_token else None
        position = self.cursor.position
        last_key: Any = None
        tpl = self.template or (cfg.request.read if cfg.request else None)
        st = Outcome("read", page_size=size)
        if tpl is None:
            self._finish(Outcome("failed", cause={"code": "source.failed", "message": "no read request"}), None, None)
            return
        number = 0
        while True:
            why = src.budget.exhausted()
            if why:
                st.status, st.position, st.reason = "partial", position, why
                st.cause = {"code": "source.budget_exhausted", "message": why}
                break
            ctx = src.context(self.unit, self.cursor, page={"size": size, "token": token, "number": number + 1},
                              limit=size, **self.extra)
            payload = render(tpl.payload(), ctx)
            resp = await src.send(payload)
            src.budget.pages += 1
            body: Any = None
            bad: dict[str, Any] | None = None
            if resp.error is None and not resp.truncated:
                body, bad = src._parse(resp)
            if src._too_large(resp, body):
                if size <= min_size:
                    st.status, st.position = "partial", position
                    st.reason = f"truncated at the minimum page size {min_size}"
                    st.cause = {"code": "source.truncated", "message": st.reason,
                                **({"transport": resp.error.as_dict()} if resp.error else {})}
                    break
                size = max(min_size, size // 2)
                st.stepdowns += 1
                st.page_size = size
                continue
            if resp.error is not None:
                kind = "unavailable" if resp.error.kind == "unavailable" and number == 0 else "failed"
                st.status, st.position, st.cause = kind, position, failure_cause(resp.error)
                if kind == "unavailable":
                    st.reason = resp.error.message
                break
            if bad is not None:
                st.status, st.position, st.cause = "failed", position, bad
                break
            rows = lookup(body, pg.entries)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                unexpected("source.contract", f"{src.id} page {pg.entries} is not a list",
                           sample=src.checker._sample(rows), reading="read the page as empty",
                           extend=f"sources.{src.id}.paging.entries")
                rows = []
            entries: list[Entry] = []
            page_key: Any = None
            failed_unit: dict[str, Any] | None = None
            for i, raw in enumerate(rows):
                checked = src.checker.check(pg.kind, raw)
                if checked.verdict == "type_changed":
                    failed_unit = {"code": "source.contract.type_changed", "message": checked.detail,
                                   "sample": checked.sample}
                    break
                key = lookup(checked.view, pg.order_key) if (checked.view is not None and pg.order_key) else None
                if checked.verdict == "tombstone":
                    st.tombstones += 1
                    key = lookup(raw, pg.order_key) if pg.order_key else None
                elif checked.verdict == "record_failed":
                    st.failed_records += 1
                entries.append(Entry(i, raw, checked.view, checked.verdict == "tombstone", key))
                if key is not None:
                    page_key = max_key(page_key, key)
            if failed_unit is not None:
                st.status, st.position, st.cause = "failed", position, failed_unit
                break
            number += 1
            nxt = _token(lookup(body, pg.next)) if pg.next else None
            ends = set(pg.end_when)
            end = False
            if "token_absent" in ends and nxt is None:
                end = True
            if "flag" in ends and pg.end and _truthy(lookup(body, pg.end)):
                end = True
            if "short_page" in ends and len(rows) < size:
                end = True
            if "empty_page" in ends and not rows:
                end = True
            if "past_interval" in ends and page_key is not None and self.cursor.interval_to:
                from docket.sources.cursors import compare
                c = compare(page_key, self.cursor.interval_to)
                end = end or (c is not None and c >= 0)
            st.pages += 1
            st.entries += sum(1 for e in entries if e.view is not None)
            last_key = max_key(last_key, page_key)
            if by_token:
                position = nxt if nxt is not None else position
            elif last_key is not None:
                position = str(last_key)
            yield Page(number, payload, resp.body, entries, nxt, end, size)
            if end:
                break
            if self.entry_limit is not None and st.entries >= self.entry_limit:
                st.status, st.position, st.reason = "partial", position, f"sample limit {self.entry_limit} reached"
                break
            if nxt is None or (by_token and nxt == token and number > 1) or \
                    render(tpl.payload(), {**ctx, "page": {"size": size, "token": nxt, "number": number + 1},
                                           "limit": size}) == payload:
                # No continuation, or one the request plan does not use: the next request would
                # repeat this one, so the read cannot progress.
                unexpected("source.paging", f"{src.id} page without continuation or end signal",
                           sample={"next": nxt, "entries": len(rows)},
                           reading="treat the unit as partial; its cursor holds at the last proven position",
                           extend=f"sources.{src.id}.paging.end_when")
                st.status, st.position, st.reason = "partial", position, "no continuation and no end signal"
                break
            token = nxt
        if st.status == "read" and st.entries == 0:
            st.status = "empty"
        self._finish(st, position, last_key)
