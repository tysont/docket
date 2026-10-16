"""``docket doctor`` (TD §15.4): check the environment and write nothing."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import DocketError, ExternalError, InputError
from docket.cli.registry import Arg, command


@command("doctor", "Check configuration, state, transports, auth, destinations and hooks; write nothing. Each "
                   "check names the workflows it affects; --workflow checks only what that workflow uses.",
         args=[Arg("--probe", "Also connect to remote endpoints (by default they are reported as configured).",
                   action="store_true"),
               Arg("--workflow", "Check only this workflow's sources, transports, destinations and hooks.")],
         effects=["none: reads configuration and state, runs auth checks and the destination programs' status "
                  "verbs unattended (stdin closed, no TTY, a timeout, DOCKET_NONINTERACTIVE=1)"],
         examples=["docket doctor --json", "docket doctor --workflow daily --json", "docket doctor --probe --json"])
def cmd_doctor(ctx: Any, ns: Any) -> Result:
    from docket.engine.doctor import Doctor, by_workflow, summarize
    try:
        root = ctx.workspace_root()
    except DocketError:
        root = None
    if ns.workflow and root is not None:
        ws = ctx.workspace()
        if ns.workflow not in ws.config.workflows:
            from docket.config.loader import closest
            names = sorted(ws.config.workflows)
            raise InputError("input.unknown_workflow", f"The workspace defines no workflow {ns.workflow!r}.",
                             hint="docket config explain workflows",
                             details={"suggestion": closest(ns.workflow, names), "workflows": names})
    doc = Doctor(root=root, env=ctx.env, probe=bool(ns.probe), workflow=ns.workflow or None)
    checks = doc.run()
    rows = [c.as_dict() for c in checks]
    counts = summarize(checks)
    workflows = [ns.workflow] if ns.workflow else sorted(doc.scope)
    per = by_workflow(checks, workflows)
    lines = [f"{c.status:<4} {c.subject}: {c.detail}" + (f"\n     fix: {c.fix}" if c.fix and c.status == "fail" else "")
             for c in checks]
    lines += [f"workflow {w}: " + ("ok" if n["fail"] == 0 else f"{n['fail']} failed check(s)")
              + (f", {n['warn']} warning(s)" if n["warn"] else "") for w, n in per.items()]
    failed = [c for c in checks if c.status == "fail"]
    scope = {"workflow": ns.workflow or None, "workflows": per}
    if failed:
        first = failed[0]
        hit = [w for w, n in per.items() if n["fail"]]
        clean = [w for w, n in per.items() if not n["fail"]]
        msg = (f"{len(failed)} check(s) failed; first: {first.subject}: {first.detail}; "
               f"affects workflow(s) {', '.join(hit) or 'none'}")
        if clean:
            msg += f" (unaffected: {', '.join(clean)}; docket doctor --workflow {clean[0]})"
        raise ExternalError("doctor.failed", msg, hint=first.fix or "docket doctor",
                            details={"checks": rows, "counts": counts, **scope})
    return Result({"checks": rows, "counts": counts, "ok": True, **scope},
                  "\n".join(lines) + f"\ndoctor: {counts['ok']} ok, {counts['warn']} warning(s), "
                                     f"{counts['skip']} skipped")
