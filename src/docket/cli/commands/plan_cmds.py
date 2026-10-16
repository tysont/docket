"""Planning inspection commands (TD §12.6, FR-PLN-08, FR-DEC-08): ``plan diff``,
``plan explain <item>`` and ``plan load <date>``. All are read-only. ``plan build``
lives with the other stage commands in ``stage_cmds``."""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from docket.cli.envelope import Result
from docket.cli.errors import ConfigError, StateError
from docket.cli.registry import Arg, command


def _plan_eval(rc: Any) -> Any:
    if "plan" not in rc.workflow.stages:
        raise StateError("stage.not_in_workflow", f"Workflow {rc.workflow_name()!r} has no plan stage.",
                         hint=" ".join(rc.argv("next")))
    return rc.eval("plan")


def _need_calendar(rc: Any) -> None:
    from docket.plan.build import planning_active
    if not planning_active(rc):
        raise ConfigError("plan.no_calendar",
                          f"Planning is not active for workflow {rc.workflow_name()!r}: set planning.calendar "
                          f"(and list the workflow in planning.workflows if that list is set).",
                          hint="docket config explain planning")


@command("plan diff", "Show the current plan's operations in human terms, grouped by day and destination.",
         run=True, examples=["docket plan diff --json"])
def cmd_plan_diff(ctx: Any, ns: Any) -> Result:
    from docket.plan.build import diff_groups
    from docket.store.paths import read_json
    rc = ctx.run_context()
    ev = _plan_eval(rc)
    if ev.state != "current" or not ev.output:
        build = rc.argv("plan", "build")
        raise StateError("plan.not_built", f"The plan is {ev.state}; there is no current plan to show.",
                         hint=" ".join(build), next_command=build, details={"state": ev.state, "reason": ev.reason})
    pd = ev.output["plan_digest"]
    pdir = rc.run.paths.plan(pd)
    plan = read_json(pdir / "plan.json")
    groups = diff_groups(plan, ZoneInfo(rc.run.header.clock.timezone))
    text = (pdir / "diff.md").read_text(encoding="utf-8")
    return Result({"plan_digest": pd, "diff_path": str(pdir / "diff.md"), "groups": groups,
                   "unschedulable": plan.get("unschedulable", []), "notes": plan.get("notes", [])}, text)


def _decide_output(rc: Any) -> dict[str, Any]:
    up = rc.upstream("plan")
    if up is None or up.state != "current" or not up.output:
        nxt = rc.argv("next")
        raise StateError("stage.not_ready", f"plan explain needs decide to be current; it is "
                                            f"{getattr(up, 'state', 'absent')}.",
                         hint=" ".join(nxt), next_command=nxt,
                         details={"blocking_stage": getattr(up, "name", None), "state": getattr(up, "state", None)})
    return up.output


@command("plan explain", "Explain where a task landed or why it could not be placed: its ordering key, "
                         "candidate days and the first violated constraint of each rejected slot.",
         run=True, args=[Arg("item", "Task id, question id, block key, or words of the task title.")],
         examples=["docket plan explain q_0123456789abcdef --json", "docket plan explain 'capacity doc' --json"])
def cmd_plan_explain(ctx: Any, ns: Any) -> Result:
    from docket.plan.build import explain, run_planning
    rc = ctx.run_context()
    _plan_eval(rc)
    _need_calendar(rc)
    built = run_planning(rc, _decide_output(rc))
    out = explain(built, ns.item)
    lines = [f"{out['title']} ({out['task_id']}), rank {out['rank']}", f"ordering: {out['ordering_key']}"]
    if "placed" in out:
        pl = out["placed"]
        lines.append(f"placed: {pl['date']} {pl['start']}-{pl['end']}" + (" [late]" if pl["late"] else "")
                     + (" [locked]" if pl["locked"] else ""))
    if "unschedulable" in out:
        lines.append(f"unschedulable: {out['unschedulable']['reason']} ({out['unschedulable']['detail']})")
    for r in out["rejected"][:30]:
        lines.append(f"  {r['date']} {r['from']}..{r['to']}: {r['constraint']} ({r['detail']})")
    return Result({"explain": out}, "\n".join(lines))


@command("plan load", "How full a day already is: committed minutes, free minutes in the work window and "
                      "existing owned blocks (FR-DEC-08).",
         run=True, args=[Arg("date", "today, tomorrow, a weekday name (next occurrence after the run date) or YYYY-MM-DD.")],
         examples=["docket plan load thu --json"])
def cmd_plan_load(ctx: Any, ns: Any) -> Result:
    from docket.engine.clock import run_date
    from docket.plan.build import calendar_ctx, day_load
    from docket.text.dates import parse_date
    rc = ctx.run_context()
    _need_calendar(rc)
    day = parse_date(ns.date, run_date(rc.run.header.clock))
    load = day_load(calendar_ctx(rc), day)
    return Result({"load": load}, f"{load['date']}: {load['committed_minutes']}m committed, {load['free_minutes']}m "
                                  f"free in {load['work_window']}, {len(load['owned_blocks'])} owned block(s)")
