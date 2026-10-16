"""``docket doctor`` (TD §15.4): check the environment and write nothing."""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import DocketError, ExternalError
from docket.cli.registry import Arg, command


@command("doctor", "Check configuration, state, transports, auth, destinations and hooks; write nothing.",
         args=[Arg("--probe", "Also connect to remote endpoints (by default they are reported as configured).",
                   action="store_true")],
         effects=["none: reads configuration and state, runs auth checks and the destination programs' status "
                  "verbs unattended (stdin closed, no TTY, a timeout, DOCKET_NONINTERACTIVE=1)"],
         examples=["docket doctor --json", "docket doctor --probe --json"])
def cmd_doctor(ctx: Any, ns: Any) -> Result:
    from docket.engine.doctor import Doctor, summarize
    try:
        root = ctx.workspace_root()
    except DocketError:
        root = None
    checks = Doctor(root=root, env=ctx.env, probe=bool(ns.probe)).run()
    rows = [c.as_dict() for c in checks]
    counts = summarize(checks)
    lines = [f"{c.status:<4} {c.subject}: {c.detail}" + (f"\n     fix: {c.fix}" if c.fix and c.status == "fail" else "")
             for c in checks]
    failed = [c for c in checks if c.status == "fail"]
    if failed:
        first = failed[0]
        raise ExternalError("doctor.failed", f"{len(failed)} check(s) failed; first: {first.subject}: {first.detail}",
                            hint=first.fix or "docket doctor", details={"checks": rows, "counts": counts})
    return Result({"checks": rows, "counts": counts, "ok": True},
                  "\n".join(lines) + f"\ndoctor: {counts['ok']} ok, {counts['warn']} warning(s), "
                                     f"{counts['skip']} skipped")
