"""``docket next`` (TD §5.2): the single next step, computed from state.

Read-only except for the lease heartbeat, which the CLI renews separately.
"""
from __future__ import annotations

from typing import Any

from docket.engine.context import RunContext
from docket.engine.stages import STAGE_IMPLS
from docket.engine.steps import Command, DoneStep, WaitStep
from docket.store import lease


def summary(rc: RunContext) -> dict[str, Any]:
    r = rc.run
    out: dict[str, Any] = {"run": r.id, "status": r.header.status, "sandbox": r.header.sandbox}
    col = rc.eval_or_none("collect")
    if col and col.output:
        out["records"] = sum(int(s.get("records", 0)) for s in col.output["sources"].values())
        out["items"] = len(col.output["items"])
    dec = rc.eval_or_none("decide")
    if dec and dec.output:
        out["questions"] = len(dec.output["questions"])
        out["answered"] = len(dec.output["answers"])
    app = rc.eval_or_none("apply")
    if app and app.output:
        out["operations"] = len(app.output["receipts"])
        out["applied"] = sum(1 for x in app.output["receipts"] if x["outcome"] == "applied")
    out["journal"] = str(r.paths.journal) if r.paths.journal.is_file() else None
    n = 0
    if r.paths.unexpected.is_file():
        n = len({ln for ln in r.paths.unexpected.read_text(encoding="utf-8").splitlines() if ln.strip()})
    out["unexpected"] = n
    return out


def _inspect(rc: RunContext, drift: list[dict[str, Any]]) -> list[Command]:
    out = [Command.of(rc.argv("run", "status"))]
    if "plan" in rc.workflow.stages:
        out.append(Command.of(rc.argv("plan", "diff")))
    for d in drift:
        for sec in STAGE_IMPLS[d["stage"]].config_sections:
            if not sec.startswith("workflow."):
                out.append(Command.of(["docket", "config", "explain", sec, "--workspace", str(rc.ws.root), "--json"]))
                break
    return out


def compute_step(rc: RunContext, *, token: str | None = None) -> Any:
    r = rc.run
    if r.header.status == "sealed":
        return DoneStep(summary=summary(rc))
    if r.header.status == "abandoned":
        return DoneStep(summary={"run": r.id, "status": "abandoned"})
    held = lease.foreign_live(r.store.db, r.id, token)
    if held is not None:
        steal = rc.argv("run", "steal", r.id)
        return WaitStep(reason="lease_held",
                        instructions=f"Session {held['owner']} (pid {held['pid']} on {held['host']}) holds run {r.id}. "
                                     f"Wait for it to finish, or take over with the steal command.",
                        resume_when="the other session's lease is released or goes stale",
                        command=Command.of(steal))
    from docket.apply.boundary import unacknowledged
    drift = unacknowledged(rc)
    if drift:
        what = "; ".join(f"{d['stage']}: {d['why']}" for d in drift)
        return WaitStep(stage=drift[0]["stage"], reason="post_apply_change",
                        instructions=f"Upstream changed after apply wrote receipts ({what}). The run does not go "
                                     f"backward: inspect the change, then finish this run on its approved plan with "
                                     f"the continue command (the change applies to the next run), or abandon it.",
                        resume_when="the operator has inspected the change",
                        command=Command.of(rc.argv("run", "continue")),
                        inspect=_inspect(rc, drift))
    ev = rc.first_pending()
    if ev is None:
        return DoneStep(summary=summary(rc))
    if ev.reason == "paused":
        rehearse = Command.of(rc.argv("run", "rehearse")) if rc.workflow.rehearse_at_pause else None
        text = (f"The {rc.workflow_name()} workflow pauses before {ev.name} until the operator's next session. "
                f"Run the resume command when the operator is ready.")
        if rehearse is not None:
            text += (" Before stopping, run the rehearse command: it drives a copy of this run to close and reports "
                     "every refusal and unexpected shape now, not in the morning.")
        return WaitStep(stage=ev.name, reason="paused", instructions=text,
                        resume_when="the operator starts the next session",
                        command=Command.of(rc.argv("run", "resume")), rehearse=rehearse)
    if ev.reason == "post_apply_change":
        return WaitStep(stage=ev.name, reason="post_apply_change",
                        instructions=f"{ev.name} changed after apply wrote receipts; the run does not go backward. "
                                     f"Inspect with docket run status, finish with docket apply and docket close, "
                                     f"or start a new run.",
                        command=Command.of(rc.argv("run", "status")))
    return STAGE_IMPLS[ev.name].step(rc)
