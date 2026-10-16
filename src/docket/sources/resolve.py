"""Resolve (TD §6.10): map a URL or key to the source that owns it and read its state.

A source claims a ref through its ``resolve.patterns`` (regular expressions; named
groups become ``{match.<name>}`` in the ``request.resolve`` template). The first
source in id order that claims the ref answers with ``state``, ``last_activity``,
``last_actor`` and ``title``. A ref no source claims is ``unresolvable``, not an
error; a claimed ref whose request fails is ``failed`` with the raw cause.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from docket.sources.base import Resolution
from docket.sources.declarative import DeclarativeSource, Limits
from docket.transports.base import Secrets


def claimant(ws: Any, ref: str) -> str | None:
    import re
    for sid in sorted(ws.config.sources):
        res = ws.config.sources[sid].resolve
        if res and any(re.search(rx, ref) for rx in res.patterns):
            return sid
    return None


async def _resolve(ws: Any, sid: str, ref: str, env: Mapping[str, str], log_dir: Path | None) -> Resolution:
    from docket.transports.registry import build_transport
    cfg = ws.config.sources[sid]
    secrets = Secrets()
    t, problem = build_transport(cfg.transport, ws.config.transports[cfg.transport], root=ws.root, env=env,
                                 log_dir=log_dir, secrets=secrets)
    if problem is not None:
        return Resolution(sid, ref, status="failed", cause=problem.as_dict())
    try:
        opened = await t.open()
        if opened is not None:
            return Resolution(sid, ref, status="failed", cause=opened.as_dict())
        src = DeclarativeSource(sid, cfg, t, secrets=secrets,
                                limits=Limits(asyncio.Semaphore(1), asyncio.Semaphore(1)), env=dict(env))
        out = await src.resolve(None, ref)
        return out or Resolution(sid, ref, status="unresolvable")
    finally:
        await t.close()


def resolve_ref(ws: Any, ref: str, *, env: Mapping[str, str], log_dir: Path | None = None) -> Resolution:
    sid = claimant(ws, ref)
    if sid is None or not ws.config.sources[sid].transport:
        return Resolution(source="", ref=ref, status="unresolvable")
    return asyncio.run(_resolve(ws, sid, ref, env, log_dir))
