"""``docket doctor`` (TD §15.4): check the environment and write nothing."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import DocketError, ExternalError, InputError
from docket.cli.registry import Arg, command


@command("doctor", "Check configuration, state, transports, auth, destinations and the commands run nodes run; "
                   "write nothing. Each check names the graphs it affects; --graph checks only what that graph uses.",
         args=[Arg("--probe", "Also connect to remote endpoints (by default they are reported as configured).",
                   action="store_true"),
               Arg("--graph", "Check only this graph's sources, transports, destinations and commands.")],
         effects=["none: reads configuration and state, runs auth checks and the destination programs' status "
                  "verbs unattended (stdin closed, no TTY, a timeout, DOCKET_NONINTERACTIVE=1)"],
         examples=["docket doctor --json", "docket doctor --graph daily --json", "docket doctor --probe --json"])
def cmd_doctor(ctx: Any, ns: Any) -> Result:
    from docket.engine.doctor import Doctor, by_graph, summarize
    try:
        root = ctx.workspace_root()
    except DocketError:
        root = None
    if ns.graph and root is not None:
        ws = ctx.workspace()
        if ns.graph not in ws.config.graphs:
            from docket.config.loader import closest
            names = sorted(ws.config.graphs)
            raise InputError("input.unknown_graph", f"The workspace defines no graph {ns.graph!r}.",
                             hint="docket config explain graphs",
                             details={"suggestion": closest(ns.graph, names), "graphs": names})
    doc = Doctor(root=root, env=ctx.env, probe=bool(ns.probe), graph=ns.graph or None)
    checks = doc.run()
    rows = [c.as_dict() for c in checks]
    counts = summarize(checks)
    graphs = [ns.graph] if ns.graph else sorted(doc.scope)
    per = by_graph(checks, graphs)
    lines = [f"{c.status:<4} {c.subject}: {c.detail}" + (f"\n     fix: {c.fix}" if c.fix and c.status == "fail" else "")
             for c in checks]
    lines += [f"graph {g}: " + ("ok" if n["fail"] == 0 else f"{n['fail']} failed check(s)")
              + (f", {n['warn']} warning(s)" if n["warn"] else "") for g, n in per.items()]
    failed = [c for c in checks if c.status == "fail"]
    scope = {"graph": ns.graph or None, "graphs": per}
    if failed:
        first = failed[0]
        hit = [g for g, n in per.items() if n["fail"]]
        clean = [g for g, n in per.items() if not n["fail"]]
        msg = (f"{len(failed)} check(s) failed; first: {first.subject}: {first.detail}; "
               f"affects graph(s) {', '.join(hit) or 'none'}")
        if clean:
            msg += f" (unaffected: {', '.join(clean)}; docket doctor --graph {clean[0]})"
        raise ExternalError("doctor.failed", msg, hint=first.fix or "docket doctor",
                            details={"checks": rows, "counts": counts, **scope})
    return Result({"checks": rows, "counts": counts, "ok": True, **scope},
                  "\n".join(lines) + f"\ndoctor: {counts['ok']} ok, {counts['warn']} warning(s), "
                                     f"{counts['skip']} skipped")
