"""Planning inspection commands (TD §12.6, FR-PLN-08, FR-DEC-08): ``plan diff``,
``plan explain <item>`` and ``plan load <date>``. All are read-only; a plan is built
by ``docket do <plan node>``."""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from docket.cli.envelope import Result
from docket.cli.errors import ConfigError, InputError, StateError
from docket.cli.registry import Arg, command

NODE_ARG = Arg("--node", "The plan node (default: the graph's only plan node, or the first that places tasks).")


def _plan_node(rc: Any, name: str | None, *, scheduling: bool = False) -> str:
    """The plan node a command is about: the one named, else the only one (or the first that
    places tasks on the calendar)."""
    nodes = rc.by_verb("plan")
    if name:
        if name not in nodes:
            raise InputError("input.bad_argument", f"{name!r} is not a plan node of graph {rc.graph_name()!r}.",
                             hint="docket graph show " + rc.graph_name(), details={"plan_nodes": nodes})
        return name
    if not nodes:
        raise StateError("node.not_in_graph", f"Graph {rc.graph_name()!r} has no plan node.",
                         hint=" ".join(rc.argv("next")), details={"nodes": list(rc.order)})
    if scheduling:
        placing = [n for n in nodes if rc.graph[n].schedule_tasks]
        if placing:
            return placing[0]
    return nodes[0]


def _need_calendar(rc: Any, node: str | None = None) -> None:
    from docket.plan.build import planning_active
    if not planning_active(rc, node):
        raise ConfigError("plan.no_calendar",
                          f"Graph {rc.graph_name()!r} places no tasks on the calendar: set planning.calendar "
                          f"and schedule_tasks on a plan node.",
                          hint="docket config explain planning")


@command("plan diff", "Show the current plan's operations in human terms, grouped by day and destination.",
         run=True, args=[NODE_ARG], examples=["docket plan diff --json"])
def cmd_plan_diff(ctx: Any, ns: Any) -> Result:
    from docket.plan.build import diff_groups
    from docket.store.paths import read_json
    rc = ctx.run_context()
    node = _plan_node(rc, ns.node)
    ev = rc.eval(node)
    if ev.state != "current" or not ev.output:
        build = rc.argv("do", node) if ev.state in ("pending", "stale") else rc.argv("next")
        raise StateError("plan.not_built", f"The plan is {ev.state}; there is no current plan to show.",
                         hint=" ".join(build), next_command=build, details={"state": ev.state, "reason": ev.reason})
    pd = ev.output["plan_digest"]
    pdir = rc.run.paths.plan(pd)
    plan = read_json(pdir / "plan.json")
    groups = diff_groups(plan, ZoneInfo(rc.run.header.clock.timezone))
    text = (pdir / "diff.md").read_text(encoding="utf-8")
    return Result({"plan_digest": pd, "diff_path": str(pdir / "diff.md"), "groups": groups,
                   "unschedulable": plan.get("unschedulable", []), "notes": plan.get("notes", [])}, text)


def _decisions(rc: Any, node: str) -> dict[str, Any]:
    from docket.engine.nodes import EMPTY_DECISIONS
    waits = [n for n in rc.inputs(node) if rc.eval(n).state != "current"]
    if waits:
        nxt = rc.argv("next")
        raise StateError("node.not_ready", f"plan explain needs what {node} works on; {', '.join(waits)} "
                                           f"{'is' if len(waits) == 1 else 'are'} not current.",
                         hint=" ".join(nxt), next_command=nxt, details={"node": node, "needs": waits})
    up = rc.upstream(node)
    return {**EMPTY_DECISIONS, **((up.output if up is not None else None) or {})}


@command("plan explain", "Explain where a task landed or why it could not be placed: its ordering key, "
                         "candidate days and the first violated constraint of each rejected slot.",
         run=True, args=[Arg("item", "Task id, question id, block key, or words of the task title."), NODE_ARG],
         examples=["docket plan explain q_0123456789abcdef --json", "docket plan explain 'capacity doc' --json"])
def cmd_plan_explain(ctx: Any, ns: Any) -> Result:
    from docket.plan.build import explain, run_planning
    rc = ctx.run_context()
    node = _plan_node(rc, ns.node, scheduling=True)
    _need_calendar(rc, node)
    built = run_planning(rc, _decisions(rc, node))
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
