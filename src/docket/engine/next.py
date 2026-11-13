"""``docket next`` (TD §5.2): every step that is ready now, computed from state.

A step belongs to a node. Nodes that do not depend on each other are ready together, and
the driver runs their steps together. Read-only except for the lease heartbeat, which the
CLI renews separately.
"""
from __future__ import annotations

from typing import Any

from docket.engine.context import RunContext
from docket.engine.steps import Command, WaitStep
from docket.store import lease


def summary(rc: RunContext) -> dict[str, Any]:
    r = rc.run
    out: dict[str, Any] = {"run": r.id, "graph": r.header.graph, "status": r.header.status, "sandbox": r.header.sandbox}
    read = rc.output("read")
    if read:
        out["records"] = sum(int(s.get("records", 0)) for s in (read.get("sources") or {}).values())
        out["items"] = len(read.get("items") or [])
    asked = rc.output("ask")
    if asked:
        out["questions"] = len(asked.get("questions") or [])
        out["answered"] = len(asked.get("answers") or {})
        out["not_asked"] = len(asked.get("not_asked") or [])  # kept, but never put to the operator
    receipts = [x for v in ("apply", "write") for x in (rc.output(v).get("receipts") or [])]
    if receipts:
        out["operations"] = len(receipts)
        out["applied"] = sum(1 for x in receipts if x["outcome"] == "applied")
        out["unwritten"] = unwritten(rc)
    out["journal"] = str(r.paths.journal) if r.paths.journal.is_file() else None
    n = 0
    if r.paths.unexpected.is_file():
        n = len({ln for ln in r.paths.unexpected.read_text(encoding="utf-8").splitlines() if ln.strip()})
    out["unexpected"] = n
    return out


def unwritten(rc: RunContext) -> list[dict[str, Any]]:
    """The operations the run's writing nodes left unwritten, as the operator or driver
    accepted them (``docket do <node> --accept-failures``), each with its node."""
    return [dict(r, node=node) for node, row in sorted(rc.run.header.unwritten.items())
            for r in row.get("operations") or []]


def _inspect(rc: RunContext, drift: list[dict[str, Any]]) -> list[Command]:
    out = [Command.of(rc.argv("run", "status"))]
    if rc.by_verb("plan"):
        out.append(Command.of(rc.argv("plan", "diff")))
    for d in drift:
        for sec in rc.impls[d["node"]].config_sections:
            out.append(Command.of(["docket", "config", "explain", sec, "--workspace", str(rc.ws.root), "--json"]))
            break
    return out


def compute_steps(rc: RunContext, *, token: str | None = None, full: bool = False) -> list[Any]:
    """Every step that is ready. An ask step is compact (AE-08) unless ``full``. Empty when
    the run is sealed, abandoned, or has nothing that can move."""
    r = rc.run
    if r.header.status in ("sealed", "abandoned"):
        return []
    held = lease.foreign_live(r.store.db, r.id, token)
    if held is not None:
        steal = rc.argv("run", "steal", r.id)
        return [WaitStep(reason="lease_held",
                         instructions=f"Session {held['owner']} (pid {held['pid']} on {held['host']}) holds run {r.id}. "
                                      f"Wait for it to finish, or take over with the steal command.",
                         ready_when="the other session's lease is released or goes stale",
                         command=Command.of(steal))]
    from docket.apply.boundary import unacknowledged
    drift = unacknowledged(rc)
    if drift:
        what = "; ".join(f"{d['node']}: {d['why']}" for d in drift)
        return [WaitStep(node=drift[0]["node"], reason="post_apply_change",
                         instructions=f"Upstream changed after a node wrote receipts ({what}). The run does not go "
                                      f"backward: inspect the change, then finish this run on what was written with "
                                      f"the continue command (the change applies to the next run), or abandon it.",
                         ready_when="the operator has inspected the change",
                         command=Command.of(rc.argv("run", "continue")),
                         inspect=_inspect(rc, drift))]
    out = []
    for ev in rc.ready():
        if ev.reason == "post_apply_change":
            out.append(WaitStep(node=ev.name, reason="post_apply_change",
                                instructions=f"{ev.name} changed after a node wrote receipts; the run does not go "
                                             f"backward. Inspect with docket run status, finish the run, or start "
                                             f"a new one.", command=Command.of(rc.argv("run", "status"))))
            continue
        step = rc.impls[ev.name].step(rc)
        out.append(step.compact() if getattr(step, "kind", None) == "ask" and not full else step)
    return out


def envelope(rc: RunContext, steps: list[Any]) -> dict[str, Any]:
    """``data`` of ``docket next`` and of every command that moves a run."""
    done = rc.run.header.status in ("sealed", "abandoned")
    return {"run": rc.run.id, "steps": [s.model_dump() for s in steps], "done": done,
            "summary": summary(rc) if done else None, "nodes": rc.node_report()}
