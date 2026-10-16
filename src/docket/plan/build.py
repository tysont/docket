"""Planning inside the plan stage (TD §12.6), plus ``plan diff``, ``plan explain`` and day load.

Planning is active for a run when ``planning.calendar`` is configured and the
run's workflow is listed in ``planning.workflows`` (or that list is absent).
Without it the plan stage behaves exactly as before.

Calendar operations are plain dicts, kept compatible with the task-store
operations the stage already emits:

```
{"op_id", "destination", "verb", "calendar", "key", "start", "end", "title", "notes",
 "kind": "deep|shallow|batch|mirror", "task_ids": [...], "late": bool,
 "from": {"start", "end"}            # move only
 "reason": "..."                      # release only
 "locked": true                       # keep of a locked block
 "transparency", "source"             # mirror holds}
```

Verbs map onto the TD §11.1 calendar verbs as ``create -> create``,
``move -> move``, ``update -> update``, ``release -> delete`` and ``keep -> no
effect`` (an explicit record that the block stays). ``op_id`` is
``op_`` + the digest of destination, verb, key and payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from docket.cli.errors import ConfigError, InputError, IntegrityError
from docket.config.digest import digest
from docket.plan.calendar_classify import Calendar, classify_calendar, clip, expand
from docket.plan.constraints import Ctx, block_for, candidate_days, check, slot_bounds, _grid_up
from docket.plan.mirrors import LEDGER, MirrorPlan, plan_mirrors
from docket.plan.providers import JsonFileProvider
from docket.plan.scheduler import ScheduleResult, describe_key, schedule, validate
from docket.plan.taskstore import (
    parse_field_date, parse_field_int, parse_field_minutes, parse_line, read_store,
)
from docket.plan.types import DAY, Policy, Task, at, hhmm
from docket.plan.windows import resolve_window
from docket.text import templates

if TYPE_CHECKING:
    from docket.engine.context import RunContext

CALENDAR_VERBS = {"create": "create", "move": "move", "update": "update", "release": "delete", "keep": None}


def planning_active(rc: "RunContext") -> bool:
    pl = rc.ws.config.planning
    if pl.calendar is None or "plan" not in rc.workflow.stages:
        return False
    return pl.workflows is None or rc.workflow_name() in pl.workflows


def store_destination(rc: "RunContext") -> str | None:
    pl = rc.ws.config.planning
    if pl.tasks.destination:
        return pl.tasks.destination
    for name in sorted(rc.ws.config.destinations):
        if rc.ws.config.destinations[name].adapter == "markdown_tasks":
            return name
    return None


@dataclass
class Inputs:
    policy: Policy
    tz: ZoneInfo
    today: date
    now: datetime
    lo: date
    hi: date
    provider: Any
    events: list[Any]
    store_tasks: list[Any]
    store_digest: str
    store_dest: str | None
    ledger: dict[str, str]


def _provider(rc: "RunContext", tz: ZoneInfo) -> Any:
    """The planner's calendar. Ownership is always the calendar destination's marker
    (``docket.destinations.calendar.owned_key``): the scheme apply writes is the scheme planning reads."""
    from docket.destinations.calendar import marker_for, owned_key
    c = rc.ws.config.planning.calendar
    assert c is not None
    dests = rc.ws.config.destinations
    marker = marker_for(dests, c.destination)

    def owner(notes: str, attendees: int, recurring: bool) -> str | None:
        return owned_key(marker, notes, attendees=attendees, recurring=recurring, dest_id=c.destination)

    if c.provider == "destination":
        from docket.destinations import build, env_for
        from docket.plan.providers import ListingProvider
        adapter = build(c.destination, dests[c.destination], env_for(rc.ws, run=rc.run, env=rc.env))
        today = date.fromisoformat(rc.run.header.clock.date)
        horizon = rc.ws.config.planning.policy.horizon_days
        window = (at(today - timedelta(days=1), 0, tz), at(today + timedelta(days=horizon + 1), 0, tz))

        def list_raw(w: tuple[str, str]) -> list[Any]:
            try:
                return adapter.backend.list(adapter.calendar, w)
            finally:
                adapter.close()

        return ListingProvider(list_raw, adapter.owner_key, tz, window=window, default_calendar=c.calendar,
                               where=f"destinations.{c.destination}", strip=adapter.marker.strip)
    return JsonFileProvider(rc.ws.root / (c.path or ""), tz, default_calendar=c.calendar,
                            key_property=c.key_property, owner=owner, strip=marker.strip)


def read_ledger(rc: "RunContext") -> dict[str, str]:
    import json
    state: dict[str, str] = {}
    for row in rc.run.store.db.execute("SELECT key, json FROM ledgers WHERE ledger=? ORDER BY id", (LEDGER,)):
        try:
            state[row["key"]] = json.loads(row["json"]).get("state", "")
        except ValueError:
            continue
    return state


def write_ledger(rc: "RunContext", updates: list[tuple[str, str]], before: dict[str, str]) -> int:
    import json
    db = rc.run.store.db
    n = 0
    with db:
        for key, st in updates:
            if before.get(key) == st:
                continue
            db.execute("INSERT INTO ledgers (ledger, run_id, key, json) VALUES (?,?,?,?)",
                       (LEDGER, rc.run.id, key, json.dumps({"state": st}, sort_keys=True)))
            n += 1
    return n


def gather(rc: "RunContext") -> Inputs:
    cfg = rc.ws.config
    clock = rc.run.header.clock
    tz = ZoneInfo(clock.timezone)
    policy = Policy.from_config(cfg.planning, list(cfg.profile.workdays))
    today = date.fromisoformat(clock.date)
    now = datetime.fromisoformat(clock.instant).astimezone(tz)
    lo, hi = today, today + timedelta(days=policy.horizon_days)
    prov = _provider(rc, tz)
    events = prov.list_events(at(lo, 0, tz), at(hi, 0, tz))
    dest = store_destination(rc)
    tasks: list[Any] = []
    sd = "absent"
    if dest is not None and dest in cfg.destinations:
        tasks, sd = read_store(rc, dest, sections=cfg.planning.tasks.sections)
    return Inputs(policy, tz, today, now, lo, hi, prov, events, tasks, sd, dest, read_ledger(rc))


def _light_instances(inp: Inputs) -> list[tuple[Any, str]]:
    exp = expand(inp.events, inp.lo, inp.hi, inp.tz)
    pre = inp.policy.key_prefix
    return [(ev, "owned" if ev.owned_key and ev.owned_key.startswith(pre) else "other") for ev in exp.events]


def input_digests(rc: "RunContext") -> dict[str, Any]:
    """What planning reads besides decide: folded into the plan stage digest (TD §4.4)."""
    from docket.shapes.unexpected import collecting
    with collecting():  # shapes are recorded by the build, not by every evaluation
        inp = gather(rc)
        mp = plan_mirrors(rc.ws.config.planning.mirrors, _light_instances(inp), prefix=inp.policy.key_prefix,
                          tz=inp.tz, ledger=inp.ledger)
    prof = rc.ws.resolved.get("profile", {})
    return {"task_store_digest": inp.store_digest, "calendar_digest": inp.provider.digest(),
            "mirror_ledger_digest": digest(sorted(mp.suppressed)),
            "profile_digest": digest({k: prof.get(k) for k in ("calendar", "workdays", "timezone")})}


# --- tasks -----------------------------------------------------------------------


def _task_from_fields(tid: str, title: str, fields: dict[str, str], inp: Inputs, *, tier: int, source: str,
                      instances: list[tuple[Any, str]], qid: str | None = None) -> Task:
    p = inp.policy
    est = parse_field_minutes(fields["est"]) if "est" in fields else None
    due = parse_field_date(fields["due"], inp.today) if "due" in fields else None
    earliest = parse_field_date(fields["start"], inp.today) if "start" in fields else None
    t_tier = parse_field_int(fields["tier"], "tier") if "tier" in fields else None
    carry = parse_field_int(fields["carry"], "carry") if "carry" in fields else None
    window = None
    if fields.get("when"):
        window = resolve_window(fields["when"], inp.today, instances=instances, lo=inp.lo, hi=inp.hi, tz=inp.tz)
    return Task(id=tid, title=title, est=est if est is not None else p.default_est, due=due, earliest=earliest,
                tier=t_tier if t_tier is not None else tier, window=window, window_text=fields.get("when"),
                carry=carry or 0, deadline=str(fields.get("deadline", "")).lower() in ("true", "yes", "1"),
                source=source, question_id=qid, defaulted_est=est is None)


def gather_tasks(rc: "RunContext", inp: Inputs, decide: dict[str, Any], instances: list[tuple[Any, str]]) -> list[Task]:
    from docket.decide.operations import plan_operations, scheduling_inputs
    out: dict[str, Task] = {}
    adjust = plan_operations(rc, decide)["adjust"] if decide.get("reconcile") or decide.get("verify") else {}
    for st in inp.store_tasks:
        adj = adjust.get(st.id) or {}
        if adj.get("closed"):  # M5: closed by a reconcile or verify answer; its blocks are released
            continue
        fields = dict(st.fields)
        if adj.get("est"):
            fields["est"] = str(adj["est"])
        out[st.id] = _task_from_fields(st.id, st.title, fields, inp, tier=st.tier, source="store",
                                       instances=instances)
    for row in scheduling_inputs(rc, decide):  # M5: finding answers, merges applied (docket.decide.operations)
        qid, ov, op = row["question_id"], row["overrides"], row["op"]
        title = row["title"]
        tid = "t_" + digest(["answer", qid])[:16]
        fields: dict[str, str] = {}
        if op is not None:
            parsed = parse_line(rc, op["destination"], op["line"])
            if parsed is not None:  # the id the store task will have once the line is written
                tid, title, fields = parsed.id, parsed.title, dict(parsed.fields)
            if ov.get("title"):  # the block carries the title override; the line carries the task text
                title = ov["title"]
        if "due" in ov:
            fields["due"] = str(ov["due"])
        elif "due" not in fields and not ov.get("when"):  # a window governs when; no default due to be late against
            fields["due"] = (inp.today + timedelta(days=inp.policy.default_lead_days)).isoformat()
        if "est" in ov:
            fields["est"] = str(ov["est"])
        if ov.get("when"):
            fields["when"] = str(ov["when"])
        out[tid] = _task_from_fields(tid, title, fields, inp, tier=0, source="answer", instances=instances, qid=qid)
    return [out[k] for k in sorted(out)]


def _titler(rc: "RunContext", by_id: dict[str, Task]):
    t = rc.ws.config.planning.titles
    tpl = {k: templates.parse(getattr(t, k)) for k in ("task", "batch", "notes", "batch_notes")}

    def f(task: Task) -> tuple[str, str]:
        members = [by_id[m] for m in task.members if m in by_id]
        ctx = {"title": task.title, "count": len(members), "est": task.est,
               "due": task.due.isoformat() if task.due else "", "window": task.window.label if task.window else "",
               "members": [{"title": m.title, "est": m.est, "id": m.id} for m in members]}
        if task.kind == "batch":
            return templates.render(tpl["batch"], ctx), templates.render(tpl["batch_notes"], ctx).rstrip()
        return templates.render(tpl["task"], ctx), templates.render(tpl["notes"], ctx)
    return f


# --- the build ------------------------------------------------------------------


@dataclass
class Built:
    inputs: Inputs
    cal: Calendar
    tasks: list[Task]
    result: ScheduleResult
    mirrors: MirrorPlan
    ops: list[dict[str, Any]]
    notes: list[str]


def run_planning(rc: "RunContext", decide: dict[str, Any]) -> Built:
    cfg = rc.ws.config
    inp = gather(rc)
    cal = classify_calendar(inp.events, cfg.profile.calendar.classify, lo=inp.lo, hi=inp.hi, tz=inp.tz,
                            key_prefix=inp.policy.key_prefix)
    mp = plan_mirrors(cfg.planning.mirrors, cal.instances, prefix=inp.policy.key_prefix, tz=inp.tz, ledger=inp.ledger)
    for ev in mp.holds:  # holds that stand after the plan are busy for the scheduler
        for seg in clip(ev, "owned", inp.lo, inp.hi, inp.tz):
            cal.days.setdefault(seg.day, []).append(seg)
    for segs in cal.days.values():
        segs.sort(key=lambda s: (s.start, s.end, s.event_id))
    tasks = gather_tasks(rc, inp, decide, cal.instances)
    by_id = {t.id: t for t in tasks}
    c = cfg.planning.calendar
    assert c is not None
    res = schedule(tasks, cal, inp.policy, today=inp.today, now=inp.now, tz=inp.tz, calendar=c.calendar,
                   titler=_titler(rc, by_id))
    problems = validate(res)
    if problems:
        raise IntegrityError("plan.invalid", "The built plan failed its own validation; this is a defect.",
                             hint="Report the details", details={"problems": problems[:50]})
    ops = []
    for op in res.ops + mp.ops:
        body = {"destination": c.destination, **op}
        body["op_id"] = "op_" + digest({k: body.get(k) for k in ("destination", "verb", "key", "calendar", "start",
                                                                  "end", "title", "notes", "transparency")})[:16]
        ops.append(body)
    ops.sort(key=lambda o: (o["start"], o["key"], o["verb"]))
    notes = []
    n_def = sum(1 for t in tasks if t.defaulted_est)
    if n_def:
        notes.append(f"{n_def} task(s) had no estimate and used the default of {inp.policy.default_est}m.")
    cc = cal.counts
    notes.append(f"Calendar: {cc['considered']} event(s) listed, {cc['concrete']} in the horizon, "
                 f"{cc['tombstones']} cancelled, {cc['generated']} recurring occurrence(s).")
    if cfg.planning.mirrors:
        m = mp.counts
        notes.append(f"Mirrors: {m['qualifying']} source event(s): {m['created']} create, {m['moved']} move, "
                     f"{m['kept']} keep, {m['suppressed']} not recreated after deletion, {m['released']} release.")
    if res.moves_used:
        notes.append(f"Repair displaced {res.moves_used} block(s) (move cap {inp.policy.move_cap}).")
    return Built(inp, cal, tasks, res, mp, ops, notes)


def raise_window_errors(rc: "RunContext", built: Built) -> None:
    errs = built.result.window_errors
    if not errs:
        return
    e = errs[0]
    qid = e.get("question_id")
    hint_argv = rc.argv("ask", "answer", qid, "<option>", "--when", "<a wider window>") if qid else None
    raise InputError("plan.window_too_small",
                     f"The window {e['window']} has {e['free_minutes']} free minute(s) (longest stretch "
                     f"{e['longest_free']}m); the block needs {e['est']}m.",
                     hint=" ".join(hint_argv) if hint_argv else "Change the task's when field to a wider window",
                     details={"windows": errs})


def extend_plan(rc: "RunContext", plan: dict[str, Any], decide: dict[str, Any], decide_digest: str | None) -> Built:
    built = run_planning(rc, decide)
    raise_window_errors(rc, built)
    c = rc.ws.config.planning.calendar
    assert c is not None
    if "apply" in rc.workflow.stages:
        from docket.config.models import IMPLEMENTED_ADAPTERS
        dest = rc.ws.config.destinations.get(c.destination)
        if dest is None or dest.adapter not in IMPLEMENTED_ADAPTERS:
            raise ConfigError("config.capability_missing",
                              f"Workflow {rc.workflow_name()!r} applies plans, but calendar destination "
                              f"{c.destination!r} has no implemented calendar adapter.",
                              hint="Remove the workflow from planning.workflows, or configure a calendar adapter",
                              details={"destination": c.destination})
    plan["inputs"].update({
        "task_store_digest": built.inputs.store_digest, "calendar_digest": built.inputs.provider.digest(),
        "answers_digest": digest(decide.get("answers") or {}), "config_digest": plan["inputs"].get("config_digest"),
        "mirror_ledger_digest": digest(sorted(built.mirrors.suppressed)),
    })
    plan["operations"] = list(plan["operations"]) + built.ops
    plan["schedule"] = built.result.schedule
    plan["unschedulable"] = built.result.unschedulable
    plan["notes"] = list(plan.get("notes") or []) + built.notes
    write_ledger(rc, built.mirrors.ledger_updates, built.inputs.ledger)
    return built


# --- diff, explain, day load ---------------------------------------------------------


def _local(iso: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(iso).astimezone(tz)


def _span(op: dict[str, Any], tz: ZoneInfo) -> str:
    a, b = _local(op["start"], tz), _local(op["end"], tz)
    if a.date() == b.date() or (b - a) <= timedelta(days=1) and b.hour == 0 and b.minute == 0:
        return f"{a:%H:%M}-{b:%H:%M}"
    return f"{a:%H:%M}-{b:%a %H:%M}"


_VERB_WORD = {"create": "new block", "move": "move", "update": "update", "keep": "keep", "release": "release"}


def diff_groups(plan: dict[str, Any], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Operations in human terms, grouped by day and destination (FR-PLN-08)."""
    groups: dict[tuple[str, str], list[str]] = {}
    for op in plan["operations"]:
        if "start" not in op:
            where = f"{op.get('path', '')} under {op.get('anchor', '')}".strip()
            if op.get("verb") == "replace":  # M5: reconcile and verify change existing lines
                text = f"change `{op.get('old_line', '')}` to `{op.get('line')}` ({where})"
            elif op.get("verb") == "draft":
                text = f"draft `{op.get('subject', '')}` to {', '.join(op.get('to') or [])}"
            else:
                text = f"add `{op.get('line')}` ({where})"
            groups.setdefault(("", op["destination"]), []).append(text)
            continue
        day = _local(op["start"], tz).date().isoformat()
        what = _VERB_WORD.get(op["verb"], op["verb"])
        if op.get("kind") == "mirror":
            what = {"create": "new hold", "move": "move hold", "keep": "keep hold", "release": "release hold"}.get(
                op["verb"], what)
        line = f"{_span(op, tz)} {what}: {op['title']}"
        if op["verb"] == "move" and op.get("from"):
            f = _local(op["from"]["start"], tz)
            line += f" (from {f:%a %H:%M})"
        if op.get("reason"):
            line += f" ({op['reason']})"
        if op.get("late"):
            line += " [late]"
        if op.get("locked"):
            line += " [locked]"
        groups.setdefault((day, op["destination"]), []).append(line)
    out = []
    for (day, dest) in sorted(groups):
        out.append({"date": day or None, "label": (f"{date.fromisoformat(day):%a %Y-%m-%d}" if day else "Task store"),
                    "destination": dest, "lines": groups[(day, dest)]})
    return out


def render_diff(plan: dict[str, Any], pd: str, tz: ZoneInfo) -> str:
    lines = [f"# Plan {pd[:12]}", ""]
    groups = diff_groups(plan, tz)
    undated = [g for g in groups if g["date"] is None]
    for g in undated + [g for g in groups if g["date"] is not None]:
        lines.append(f"## {g['label']}: {g['destination']}")
        lines += [f"- {x}" for x in g["lines"]]
        lines.append("")
    if not groups:
        lines += ["- No operations.", ""]
    if plan.get("unschedulable"):
        lines.append("## Not scheduled")
        for u in plan["unschedulable"]:
            lines.append(f"- {u['title']}: {u['reason']} ({u['detail']})")
        lines.append("")
    if plan.get("outcomes"):  # M5: answers that write nothing (dispositions are recorded at close)
        lines.append("## Decisions without operations")
        for r in plan["outcomes"]:
            lines.append(f"- {r['question']}: {r['decision']}" + (f" until {r['until']}" if r.get("until") else ""))
        lines.append("")
    if plan.get("notes"):
        lines.append("## Notes")
        lines += [f"- {n}" for n in plan["notes"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _find_unit(built: Built, ref: str):
    u = built.result.unit_for(ref)
    if u is not None:
        return u
    low = ref.casefold()
    hits = [x for x in built.result.units if low in x.task.title.casefold()
            or any(low in (t.title.casefold()) for t in built.tasks if t.id in x.task.members)]
    return hits[0] if len(hits) == 1 else (None if not hits else hits)


def explain(built: Built, ref: str, *, max_ranges: int = 200) -> dict[str, Any]:
    found = _find_unit(built, ref)
    if found is None or isinstance(found, list):
        cands = [x.task.title for x in found] if isinstance(found, list) else []
        raise InputError("plan.unknown_item", f"No planned task matches {ref!r}" +
                         (f" uniquely ({len(cands)} match)" if cands else "") + ".",
                         hint="docket plan explain <task id, question id, block key or title words>",
                         details={"candidates": cands[:20],
                                  "known": [x.task.id for x in built.result.units][:50]})
    u = found
    res = built.result
    p = res.ctx.policy
    before = [x.block for x in res.units if x.block is not None and x is not u and (u.seq < 0 or x.seq < u.seq)]
    c = res.ctx.copy(overlay=before)
    on_time, late = candidate_days(u.task, c)
    target = (u.block.day, u.block.start) if u.block is not None else None
    ranges: list[dict[str, Any]] = []
    n = p.length_for(u.task)
    done = False
    for day in on_time + late:
        for lo, hi in slot_bounds(u.task, c, day):
            t = _grid_up(lo, p.granularity)
            while t + n <= hi:
                if target == (day, t):
                    done = True
                    break
                v = check(block_for(u.task, c, day, t), c)
                if v:
                    last = ranges[-1] if ranges else None
                    if last and last["date"] == day.isoformat() and last["constraint"] == v[0].code \
                            and last["_end"] == t:
                        last["to"], last["_end"], last["slots"] = hhmm(t + n), t + p.granularity, last["slots"] + 1
                    else:
                        ranges.append({"date": day.isoformat(), "from": hhmm(t), "to": hhmm(t + n),
                                       "constraint": v[0].code, "detail": v[0].detail, "slots": 1,
                                       "_end": t + p.granularity})
                t += p.granularity
            if done:
                break
        if done:
            break
    for r in ranges:
        r.pop("_end", None)
    out: dict[str, Any] = {
        "task_id": u.task.id, "title": u.task.title, "key": u.key, "kind": p.block_kind(u.task),
        "members": list(u.task.members), "ordering_key": describe_key(u.task, p), "rank": u.rank,
        "candidate_days": [d.isoformat() for d in on_time], "late_days": [d.isoformat() for d in late],
        "rejected": ranges[:max_ranges], "rejected_truncated": len(ranges) > max_ranges,
    }
    if u.block is not None:
        b = u.block
        out["placed"] = {"date": b.day.isoformat(), "start": hhmm(b.start), "end": hhmm(b.end), "late": u.late,
                         "locked": u.locked, "existing": u.existing is not None}
    if u.reason is not None:
        out["unschedulable"] = {"reason": u.reason.reason, "detail": u.reason.detail}
    if u.window_error is not None:
        out["window_error"] = u.window_error
    return out


def calendar_ctx(rc: "RunContext") -> Ctx:
    """The calendar as it stands, with existing owned blocks as the overlay (no tasks placed)."""
    from docket.plan.scheduler import _existing
    from docket.plan.types import Block
    inp = gather(rc)
    cal = classify_calendar(inp.events, rc.ws.config.profile.calendar.classify, lo=inp.lo, hi=inp.hi, tz=inp.tz,
                            key_prefix=inp.policy.key_prefix)
    pre = inp.policy.key_prefix
    for ev, _ in cal.owned:
        if ev.owned_key and ev.owned_key.startswith(pre + "m_"):
            for seg in clip(ev, "owned", inp.lo, inp.hi, inp.tz):
                cal.days.setdefault(seg.day, []).append(seg)
    ctx = Ctx(inp.policy, cal, inp.today, inp.now, inp.tz)
    for i, x in enumerate(_existing(cal, inp.tz, pre, pre + "m_")):
        key = x.key if x.key not in ctx.overlay.get(x.day, {}) else f"{x.key}#{i}"
        ctx.add(Block(key=key, kind="shallow", day=x.day, start=x.start, end=x.end, title=x.event.title))
    return ctx


def day_load(ctx: Ctx, day: date) -> dict[str, Any]:
    """FR-DEC-08: how full a day already is, inside the work window."""
    p = ctx.policy
    lo, hi = p.day_window
    busy: list[tuple[int, int]] = []
    for s in ctx.cal.segments(day):
        if s.kind in p.busy_kinds:
            busy.append((max(lo, s.start), min(hi, s.end)))
    owned = sorted(ctx.blocks(day), key=lambda b: b.start)
    busy += [(max(lo, b.start), min(hi, b.end)) for b in owned]
    merged: list[list[int]] = []
    for a, b in sorted(x for x in busy if x[1] > x[0]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    committed = sum(b - a for a, b in merged)
    free = ctx.free(day) if day.weekday() in p.workdays else []
    return {"date": day.isoformat(), "workday": day.weekday() in p.workdays,
            "work_window": f"{hhmm(lo)}-{hhmm(hi)}", "committed_minutes": committed,
            "free_minutes": sum(b - a for a, b in free),
            "owned_blocks": [{"key": b.key, "title": b.title, "start": hhmm(b.start), "end": hhmm(b.end)}
                             for b in owned]}


__all__ = ["planning_active", "input_digests", "extend_plan", "run_planning", "render_diff", "diff_groups",
           "explain", "day_load", "CALENDAR_VERBS", "Built", "DAY"]
