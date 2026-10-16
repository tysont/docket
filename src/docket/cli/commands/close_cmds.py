"""M8 commands: ``run continue`` (finish a run on its approved plan after a post-apply change, TD §5.4)."""
from __future__ import annotations

from typing import Any

from docket.cli.commands.run_cmds import _guard_mutable, _step_next, step_summary
from docket.cli.envelope import Result
from docket.cli.registry import command


@command("run continue", "Finish a run on its approved plan after upstream changed past the apply boundary.",
         run=True, mutating=True,
         effects=["records in run.json that the operator saw the post-apply change"],
         examples=["docket run continue --json"])
def cmd_run_continue(ctx: Any, ns: Any) -> Result:
    from docket.apply.boundary import drift_digest, unacknowledged
    from docket.engine.next import compute_step
    rc = ctx.run_context()
    rows = unacknowledged(rc)
    if not rows:
        step = compute_step(rc, token=ctx.token(rc, create=False))
        return Result({"run": rc.run.id, "acknowledged": [], "changed": False, "step": step.model_dump()},
                      f"nothing to continue past\n{step_summary(step)}", next=_step_next(step))
    _guard_mutable(rc)
    if ctx.dry_run:
        return Result({"dry_run": True, "would_acknowledge": rows}, f"would continue past {len(rows)} change(s)")
    ctx.acquire(rc)
    rc.run.header.drift_ack = drift_digest(rows)
    rc.run.save_header()
    rc.invalidate()
    step = compute_step(rc, token=ctx.token(rc, create=False))
    return Result({"run": rc.run.id, "acknowledged": rows, "changed": True, "step": step.model_dump()},
                  f"continuing on the approved plan\n{step_summary(step)}", next=_step_next(step))
