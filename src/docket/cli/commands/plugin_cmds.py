"""Plugin commands: ``plugin list`` and ``plugin check`` (TD §17.3)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import ConfigError, ExternalError, InputError
from docket.cli.registry import Arg, command


def _workspace_or_none(ctx: Any) -> Any:
    try:
        return ctx.workspace()
    except ConfigError as e:
        if e.code == "workspace.not_found":
            return None
        raise


def _entry_points() -> list[dict[str, str]]:
    from importlib.metadata import entry_points
    try:
        eps = entry_points(group="docket.plugins")
    except Exception:
        return []
    return [{"name": ep.name, "value": ep.value} for ep in sorted(eps, key=lambda e: e.name)]


@command("plugin list", "List destination adapters, configured destinations and installed plugins.",
         examples=["docket plugin list --json"])
def cmd_plugin_list(ctx: Any, ns: Any) -> Result:
    from docket.destinations import ADAPTERS, adapter_class
    adapters = []
    for name in sorted(ADAPTERS):
        cls = adapter_class(name)
        adapters.append({"name": name, "kind": "destination", "capabilities": sorted(cls.capabilities),
                         "verbs": sorted(cls.verbs), "builtin": True})
    ws = _workspace_or_none(ctx)
    dests = []
    if ws is not None:
        for did, cfg in sorted(ws.config.destinations.items()):
            row: dict[str, Any] = {"id": did, "adapter": cfg.adapter, "visibility": cfg.visibility}
            if cfg.program:
                row["program"] = list(cfg.program)
            dests.append(row)
    plugins = _entry_points()
    lines = ["adapters:"] + [f"  {a['name']:<16} {', '.join(a['capabilities']) or '-'}" for a in adapters]
    if dests:
        lines += ["destinations:"] + [f"  {d['id']:<16} {d['adapter']} ({d['visibility']})" for d in dests]
    if plugins:
        lines += ["plugins:"] + [f"  {p['name']:<16} {p['value']}" for p in plugins]
    return Result({"adapters": adapters, "destinations": dests, "plugins": plugins}, "\n".join(lines))


@command("plugin check", "Run the conformance suite for a configured destination or a built-in adapter, on a "
         "scratch target only.",
         args=[Arg("name", "A configured destination id, or a built-in adapter name.")],
         effects=["starts exec programs in their scratch mode; writes only to a temporary scratch directory"],
         examples=["docket plugin check backlog --json", "docket plugin check markdown_tasks --json"])
def cmd_plugin_check(ctx: Any, ns: Any) -> Result:
    from docket.destinations import ADAPTERS, conformance
    ws = _workspace_or_none(ctx)
    name = ns.name
    with tempfile.TemporaryDirectory(prefix="docket-plugin-check-") as tmp:
        scratch = Path(tmp)
        if ws is not None and name in ws.config.destinations:
            cfg = ws.config.destinations[name]
            kw: dict[str, Any] = {"profile": ws.config.profile}
            if cfg.adapter == "calendar_exec":
                kw.update({"env": ctx.env, "root": ws.root, "log_dir": scratch / "logs"})
            rep = conformance.check(cfg.adapter, cfg, scratch, name, **kw)
        elif name in ADAPTERS:
            if name == "calendar_exec":
                raise InputError("plugin.unknown", "calendar_exec needs a program; name a configured calendar_exec "
                                 "destination instead.", hint="docket plugin list")
            rep = conformance.check(name, conformance.builtin_config(name), scratch, name)
        else:
            from docket.config.loader import closest
            known = sorted(ADAPTERS) + (sorted(ws.config.destinations) if ws is not None else [])
            raise InputError("plugin.unknown", f"No built-in adapter or configured destination {name!r}.",
                             hint="docket plugin list", details={"suggestion": closest(name, known), "known": known})
    body = rep.as_dict()
    text = "\n".join(f"{'skip' if c.skipped else ('ok' if c.ok else 'FAIL'):<4} {c.name}"
                     + (f": {c.detail}" if c.detail and (not c.ok or c.skipped) else "") for c in rep.checks)
    if not rep.ok:
        raise ExternalError("plugin.check_failed", f"{rep.subject}: "
                            f"{sum(1 for c in rep.checks if not c.ok)} conformance check(s) failed",
                            hint=f"docket plugin check {name}", details={"report": body})
    return Result({"report": body}, f"{rep.subject}: conformance passed\n{text}")

