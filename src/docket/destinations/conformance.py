"""The destination conformance suite (TD §17.3, Test Plan §7.3).

``run(scenario)`` checks one adapter and its twin on a scratch target:

- ``capabilities``: declared action kinds and verbs are known names;
- ``preview_determinism``: two snapshots digest equal and two previews of the
  same action produce the same operations and ids;
- ``twin_no_effect``: driving the twin leaves the scratch target unchanged;
- ``apply_and_verify``: every operation applies and verifies;
- ``idempotent_apply``: repeating each operation id at once is
  ``already_present`` and changes nothing;
- ``twin_parity``: the same sequence leaves twin and scratch target in the
  same normalized state;
- ``unowned_refused``: operations aimed at objects Docket does not own refuse
  with ``dest.unowned`` or ``dest.line_changed`` and change nothing;
- ``capabilities_match``: every declared kind was exercised, every undeclared
  kind is not accepted, and an undeclared verb refuses with ``dest.unsupported``.

Scenarios for every built-in adapter live here, so ``docket plugin check`` and
the test suite run the same code. A scenario never touches a live target:
Markdown and ICS adapters run in a scratch directory, exec programs in their
declared ``scratch`` mode, MCP adapters against an in-memory fake server.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from docket.cli.errors import DocketError, InputError, IntegrityError
from docket.config.models import Destination as DestConfig
from docket.config.models import Profile
from docket.destinations import build
from docket.destinations.base import ACTION_KINDS, VERBS, Action, DestEnv, DestState, Operation, make_op
from docket.destinations.markdown_io import line_digest
from docket.text.grammar import Grammar

Step = Callable[[DestState], Action]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "skipped": self.skipped, "detail": self.detail}


@dataclass
class Report:
    subject: str
    adapter: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "adapter": self.adapter, "ok": self.ok,
                "checks": [c.as_dict() for c in self.checks]}

    def add(self, name: str, ok: bool, detail: str = "", skipped: bool = False) -> None:
        self.checks.append(Check(name, ok, detail, skipped))


@dataclass
class Scenario:
    subject: str
    make: Callable[[], Any]                                   # live adapter on a seeded scratch target
    steps: list[Step]
    unowned: Callable[[Any, DestState], list[Operation]]
    normalize: Callable[[DestState], Any]
    skip: str | None = None                                   # reason the mutating checks cannot run
    cleanup: Callable[[], None] | None = None


# --- the runner -----------------------------------------------------------------


def _drive(dest: Any, steps: list[Step]) -> tuple[list[tuple[str, str]], list[str], set[str]]:
    """Apply every step's operations twice each. Returns (verb, outcome) pairs, problems and kinds exercised."""
    log: list[tuple[str, str]] = []
    problems: list[str] = []
    kinds: set[str] = set()
    for step in steps:
        state = dest.snapshot()
        action = step(state)
        kinds.add(action.kind)
        for op in dest.preview(action, state):
            rec = dest.apply(op)
            vr = dest.verify(op, rec)
            log.append((op.verb, rec.outcome))
            if rec.outcome != "applied":
                problems.append(f"{action.kind}/{op.verb}: first apply returned {rec.outcome}")
            if not vr.ok:
                problems.append(f"{action.kind}/{op.verb}: verify {vr.status} {vr.detail or ''}".strip())
            before = dest.snapshot().digest
            again = dest.apply(op)
            if again.outcome != "already_present" or dest.snapshot().digest != before:
                problems.append(f"{action.kind}/{op.verb}: repeated op id was {again.outcome}, not idempotent")
    return log, problems, kinds


def run(scn: Scenario) -> Report:
    live = scn.make()
    rep = Report(scn.subject, live.adapter)
    twin = None
    try:
        caps_ok = set(live.capabilities) <= set(ACTION_KINDS) and set(live.verbs) <= set(VERBS)
        rep.add("capabilities", caps_ok and bool(live.capabilities),
                f"kinds={sorted(live.capabilities)} verbs={sorted(live.verbs)}")
        if scn.skip:
            for name in ("preview_determinism", "twin_no_effect", "apply_and_verify", "idempotent_apply",
                         "twin_parity", "unowned_refused", "capabilities_match"):
                rep.add(name, True, scn.skip, skipped=True)
            return rep
        s1, s2 = live.snapshot(), live.snapshot()
        a0 = scn.steps[0](s1)
        p1 = [o.dump() for o in live.preview(a0, s1)]
        p2 = [o.dump() for o in live.preview(a0, s2)]
        rep.add("preview_determinism", s1.digest == s2.digest and p1 == p2 and bool(p1),
                "" if p1 == p2 else "previews differ")
        before = live.snapshot().digest
        twin = live.sandbox()
        try:
            t_log, t_problems, _ = _drive(twin, scn.steps)
        except DocketError as e:
            t_log, t_problems = [], [f"twin: {e.code}: {e.message}"]
        rep.add("twin_no_effect", live.snapshot().digest == before and getattr(twin, "is_twin", False),
                "" if live.snapshot().digest == before else "driving the twin changed the scratch target")
        try:
            l_log, l_problems, kinds = _drive(live, scn.steps)
        except DocketError as e:
            l_log, l_problems, kinds = [], [f"live: {e.code}: {e.message}"], set()
        apply_problems = [p for p in l_problems + t_problems if "not idempotent" not in p]
        idem_problems = [p for p in l_problems + t_problems if "not idempotent" in p]
        rep.add("apply_and_verify", not apply_problems and bool(l_log), "; ".join(apply_problems[:5]))
        rep.add("idempotent_apply", not idem_problems and bool(l_log), "; ".join(idem_problems[:5]))
        same = t_log == l_log and scn.normalize(twin.snapshot()) == scn.normalize(live.snapshot())
        rep.add("twin_parity", same, "" if same else "twin and scratch target differ after the same operations")
        _unowned(rep, live, scn)
        _capabilities_match(rep, live, kinds)
    finally:
        if twin is not None:
            twin.close()
        live.close()
        if scn.cleanup:
            scn.cleanup()
    return rep


def _unowned(rep: Report, live: Any, scn: Scenario) -> None:
    state = live.snapshot()
    ops = scn.unowned(live, state)
    if not ops:
        rep.add("unowned_refused", True, "the target holds no object Docket could mistake for its own", skipped=True)
        return
    bad = []
    for op in ops:
        try:
            rec = live.apply(op)
            bad.append(f"{op.verb} on an unowned object returned {rec.outcome}")
        except DocketError as e:
            if e.code not in ("dest.unowned", "dest.line_changed"):
                bad.append(f"{op.verb}: refused with {e.code}, expected dest.unowned or dest.line_changed")
    changed = live.snapshot().digest != state.digest
    if changed:
        bad.append("an unowned-object operation changed the target")
    rep.add("unowned_refused", not bad, "; ".join(bad))


def _capabilities_match(rep: Report, live: Any, kinds: set[str]) -> None:
    bad = [f"declared {k} was not exercised" for k in sorted(set(live.capabilities) - kinds)]
    for k in ACTION_KINDS:
        if k not in live.capabilities and live.can_accept(Action(kind=k, destination=live.id)).ok:
            bad.append(f"accepts undeclared {k}")
    missing = [v for v in VERBS if v not in live.verbs]
    if missing:
        op = make_op(live.id, missing[0], ident="probe", path="probe", anchor="probe", line="probe", key="probe")
        try:
            live.apply(op)
            bad.append(f"undeclared verb {missing[0]} was applied")
        except DocketError as e:
            if e.code != "dest.unsupported":
                bad.append(f"undeclared verb {missing[0]} refused with {e.code}")
    rep.add("capabilities_match", not bad, "; ".join(bad))


# --- scenario builders -------------------------------------------------------------

PROFILE = Profile.model_validate({"me": {"name": "Conformance Operator"}})
TIMES = [("2030-01-08T15:00:00Z", "2030-01-08T16:00:00Z"), ("2030-01-09T10:00:00Z", "2030-01-09T10:30:00Z"),
         ("2030-01-10T13:00:00Z", "2030-01-10T14:00:00Z")]


def _ctx(title: str, **kw: Any) -> dict[str, Any]:
    base = {"title": title, "source": "conformance", "due": "2030-01-15", "est": 30, "url": "https://example.invalid/i/1",
            "text": "Synthetic evidence text.", "note": "Synthetic note.", "area": "General", "from": "",
            "fields": {}, "run": {"id": "conformance", "date": "2030-01-07"}, "question_id": "q_conformance"}
    base.update(kw)
    return base


def _find(state: DestState, needle: str) -> str:
    for ln in (state.data.get("text") or "").splitlines():
        if needle in ln:
            return ln
    raise IntegrityError("dest.state_changed", f"conformance: the scratch target lost the seeded line {needle!r}",
                         hint="docket plugin check", details={"needle": needle})


def _text_norm(state: DestState) -> Any:
    d = dict(state.data)
    d.pop("path", None)
    return d


def tasks_scenario(cfg: DestConfig, scratch: Path, dest_id: str = "tasks", profile: Any = None) -> Scenario:
    g = Grammar.from_config(cfg.grammar)
    rel = cfg.path or f"{dest_id}.md"
    anchor = cfg.section or "## Inbox"
    outside = "## Conformance outside"
    seed = "\n".join([anchor, g.render(g.new("Seeded task to carry")), "", outside,
                      g.render(g.new("A line outside the anchor")), ""])
    (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
    (scratch / rel).write_text(seed, encoding="utf-8")
    denv = DestEnv(root=scratch, profile=profile or PROFILE, sandbox_dir=scratch / ".twin", date="2030-01-07")
    tier = next(iter(cfg.sections), None)

    steps: list[Step] = [
        lambda s: Action(kind="task.add", destination=dest_id, fields=_ctx("Write the conformance summary")),
        lambda s: Action(kind="task.add", destination=dest_id,
                         fields=_ctx("Plan the conformance review", **({"tier": tier} if tier else {}))),
        lambda s: Action(kind="task.carry", destination=dest_id, target=_find(s, "Seeded task to carry"),
                         fields={"anchor": anchor}),
        lambda s: Action(kind="task.update", destination=dest_id, target=_find(s, "Seeded task to carry"),
                         fields={"anchor": anchor, "set": {"due": "2030-01-20"}}),
        lambda s: Action(kind="task.close", destination=dest_id, target=_find(s, "Write the conformance summary")),
    ]

    def unowned(dest: Any, state: DestState) -> list[Operation]:
        line = _find(state, "A line outside the anchor")
        return [
            make_op(dest_id, "replace", ident=anchor, path=rel, anchor=anchor, old_digest=line_digest(line),
                    old_line=line, line=g.close(line)),
            make_op(dest_id, "append", ident=anchor, path="not-owned.md", anchor=anchor, line=line),
        ]

    return Scenario(f"{dest_id} (markdown_tasks)", lambda: build(dest_id, cfg, denv), steps, unowned, _text_norm)


def agenda_scenario(cfg: DestConfig, scratch: Path, dest_id: str = "agendas", profile: Any = None) -> Scenario:
    base = cfg.path or "agendas"
    section = cfg.section or "## Next"
    rel = f"{base}/conformance-sync.md"
    seed = "\n".join(["# Conformance sync", "", section, "- Seeded topic", "", "## 2030-01-01", "- Old topic", "",
                      "## Parking lot", "- Unknown section line", ""])
    (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
    (scratch / rel).write_text(seed, encoding="utf-8")
    cfg = cfg.model_copy(update={"path": base, "pattern": "conformance-*.md", "capacity": None})
    denv = DestEnv(root=scratch, profile=profile or PROFILE, sandbox_dir=scratch / ".twin")
    steps: list[Step] = [
        lambda s: Action(kind="agenda.add", destination=dest_id, fields=_ctx("First conformance topic",
                                                                              agenda="conformance-sync")),
        lambda s: Action(kind="agenda.add", destination=dest_id, fields=_ctx("Second conformance topic",
                                                                              agenda="conformance-sync")),
    ]

    def unowned(dest: Any, state: DestState) -> list[Operation]:
        return [make_op(dest_id, "append", ident=section, path="elsewhere/other.md", anchor=section, line="- x")]

    def norm(state: DestState) -> Any:
        return state.data

    return Scenario(f"{dest_id} (markdown_agenda)", lambda: build(dest_id, cfg, denv), steps, unowned, norm)


def journal_scenario(cfg: DestConfig, scratch: Path, dest_id: str = "journal") -> Scenario:
    rel = cfg.path or f"{dest_id}.md"
    (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
    (scratch / rel).write_text("# Journal\n\n## 2030-01-01 earlier\n- An earlier entry\n", encoding="utf-8")
    denv = DestEnv(root=scratch, profile=PROFILE, sandbox_dir=scratch / ".twin")
    steps: list[Step] = [
        lambda s: Action(kind="journal.write", destination=dest_id,
                         fields={"heading": "## 2030-01-07 run-a", "body": "- First line\n- Second line"}),
        lambda s: Action(kind="journal.write", destination=dest_id,
                         fields={"heading": "## 2030-01-08 run-b", "body": "- Another entry"}),
    ]

    def unowned(dest: Any, state: DestState) -> list[Operation]:
        return [make_op(dest_id, "write_section", ident="## 2030-01-01 earlier", path=rel,
                        anchor="## 2030-01-01 earlier", line="- Rewritten"),
                make_op(dest_id, "write_section", ident="## x", path="other.md", anchor="## x", line="- x")]

    return Scenario(f"{dest_id} (journal)", lambda: build(dest_id, cfg, denv), steps, unowned, _text_norm)


def _cal_steps(dest_id: str) -> list[Step]:
    return [
        lambda s: Action(kind="calendar.place", destination=dest_id, key="block-a",
                         fields=_ctx("Conformance block A", start=TIMES[0][0], end=TIMES[0][1])),
        lambda s: Action(kind="calendar.place", destination=dest_id, key="block-b",
                         fields=_ctx("Conformance block B", start=TIMES[1][0], end=TIMES[1][1])),
        lambda s: Action(kind="calendar.move", destination=dest_id, key="block-a",
                         fields={"start": TIMES[2][0], "end": TIMES[2][1]}),
        lambda s: Action(kind="calendar.update", destination=dest_id, key="block-b",
                         fields=_ctx("Conformance block B, renamed")),
        lambda s: Action(kind="calendar.rekey", destination=dest_id, key="block-a", fields={"new_seed": "block-a2"}),
        lambda s: Action(kind="calendar.release", destination=dest_id, key="block-b"),
    ]


def _cal_unowned(dest: Any, state: DestState) -> list[Operation]:
    ids = list(state.data.get("unowned_ids") or [])[:2]
    return [make_op(dest.id, "move", ident="dk_probe", calendar=dest.calendar, key="dk_probe", external_id=i,
                    start=TIMES[0][0], end=TIMES[0][1]) for i in ids] + \
        [make_op(dest.id, "delete", ident="dk_probe", calendar=dest.calendar, key="dk_probe", external_id=i)
         for i in ids]


def _cal_norm(state: DestState) -> Any:
    owned = {k: {kk: vv for kk, vv in v.items() if kk != "id"} for k, v in (state.data.get("owned") or {}).items()}
    return {"owned": owned, "busy": sorted([b[0], b[1]] for b in state.data.get("busy") or [])}


UNOWNED_ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Conformance//EN",
    "BEGIN:VEVENT", "UID:meeting-1@conformance", "DTSTART:20300107T090000Z", "DTEND:20300107T093000Z",
    "SUMMARY:Team standup", "ATTENDEE:mailto:a@team.example", "END:VEVENT",
    "BEGIN:VEVENT", "UID:series-1@conformance", "DTSTART:20300107T110000Z", "DTEND:20300107T120000Z",
    "SUMMARY:Lookalike series", "DESCRIPTION:[docket:dk_aaaaaaaaaaaa]", "RRULE:FREQ=WEEKLY", "END:VEVENT",
    "END:VCALENDAR", ""])


def ics_scenario(cfg: DestConfig, scratch: Path, dest_id: str = "calendar", profile: Any = None) -> Scenario:
    rel = cfg.path or f"{dest_id}.ics"
    (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
    (scratch / rel).write_text(UNOWNED_ICS, encoding="utf-8")
    denv = DestEnv(root=scratch, profile=profile or PROFILE)
    return Scenario(f"{dest_id} (calendar_file)", lambda: build(dest_id, cfg.model_copy(update={"path": rel}), denv),
                    _cal_steps(dest_id), _cal_unowned, _cal_norm)


def exec_scenario(cfg: DestConfig, root: Path, dest_id: str = "calendar", env: Any = None,
                  profile: Any = None, log_dir: Path | None = None) -> Scenario:
    """The program runs in its declared scratch mode; a program without one gets the handshake only."""
    from docket.destinations.calendar_exec import CalendarExec, make_client
    denv = DestEnv(root=root, profile=profile or PROFILE, env=dict(env or {}), log_dir=log_dir)
    probe = make_client(dest_id, cfg, denv, "scratch")
    hello: dict[str, Any] = {}
    skip = None
    try:
        hello = probe.start()
    except DocketError as e:
        if e.code != "dest.unavailable" or "hello" not in e.details:
            raise
        hello = e.details["hello"]
        skip = "the program does not declare a scratch target; only the handshake ran"
    finally:
        probe.close()

    def make() -> Any:
        if skip:
            return _HandshakeOnly(dest_id, cfg, hello)
        return CalendarExec(dest_id, cfg, denv, mode="scratch")

    return Scenario(f"{dest_id} (calendar_exec)", make, _cal_steps(dest_id), _cal_unowned, _cal_norm, skip=skip)


class _HandshakeOnly:
    """Stands in for an exec adapter whose program has no scratch target: capabilities from hello only."""
    adapter = "calendar_exec"
    is_twin = False

    def __init__(self, dest_id: str, cfg: Any, hello: dict[str, Any]) -> None:
        from docket.destinations.calendar_exec import WRITE_VERBS
        self.id = dest_id
        self.verbs = frozenset(set(hello.get("capabilities") or []) & set(WRITE_VERBS))
        self.capabilities = frozenset({"calendar.place"} if "create" in self.verbs else set())

    def close(self) -> None:
        return None


class FakeMcp:
    """An in-memory MCP server for calendar and draft tools; ``call(name, args)`` is the transport seam."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = {e["id"]: dict(e) for e in events or []}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.n = 0
        self.calls: list[str] = []

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "list_events":
            return {"events": [copy.deepcopy(e) for _, e in sorted(self.events.items())]}
        if name == "create_event":
            self.n += 1
            eid = f"mcp-{self.n}"
            self.events[eid] = {"id": eid, "title": args["title"], "start": args["start"], "end": args["end"],
                                "notes": args.get("notes", "")}
            return {"id": eid}
        if name in ("move_event", "update_event", "delete_event"):
            ev = self.events[args["id"]]
            if name == "move_event":
                ev["start"], ev["end"] = args["start"], args["end"]
            elif name == "update_event":
                ev["title"], ev["notes"] = args["title"], args["notes"]
            else:
                del self.events[args["id"]]
            return {"ok": True}
        if name == "list_drafts":
            return {"drafts": [copy.deepcopy(d) for _, d in sorted(self.drafts.items())]}
        if name == "create_draft":
            self.n += 1
            did = f"draft-{self.n}"
            self.drafts[did] = {"id": did, **copy.deepcopy(args)}
            return {"id": did}
        raise KeyError(name)  # raise-ok: in-memory test double; an unknown tool name is a harness programming error


MCP_CAL_TOOLS = {"list": "list_events", "create": "create_event", "move": "move_event", "update": "update_event",
                 "delete": "delete_event"}
MCP_DRAFT_TOOLS = {"list": "list_drafts", "draft": "create_draft"}


def mcp_calendar_scenario(cfg: DestConfig, dest_id: str = "calendar", server: FakeMcp | None = None) -> Scenario:
    srv = server or FakeMcp([
        {"id": "m-1", "title": "Team standup", "start": "2030-01-07T09:00:00Z", "end": "2030-01-07T09:30:00Z",
         "notes": "", "attendees": ["a@team.example"]},
        {"id": "m-2", "title": "Lookalike", "start": "2030-01-07T11:00:00Z", "end": "2030-01-07T12:00:00Z",
         "notes": "[docket:dk_aaaaaaaaaaaa]", "recurrence": True}])
    denv = DestEnv(root=Path("."), profile=PROFILE, call_tool=lambda _id: srv)
    return Scenario(f"{dest_id} (calendar_mcp)", lambda: build(dest_id, cfg, denv), _cal_steps(dest_id),
                    _cal_unowned, _cal_norm)


def mcp_message_scenario(cfg: DestConfig, dest_id: str = "drafts", server: FakeMcp | None = None) -> Scenario:
    srv = server or FakeMcp()
    srv.drafts["d-0"] = {"id": "d-0", "to": ["x@team.example"], "subject": "Someone else's draft", "body": "Hi"}
    denv = DestEnv(root=Path("."), profile=PROFILE, call_tool=lambda _id: srv)
    steps: list[Step] = [
        lambda s: Action(kind="message.draft", destination=dest_id, key="msg-a",
                         fields=_ctx("Conformance follow-up", to=["y@team.example"])),
        lambda s: Action(kind="message.draft", destination=dest_id, key="msg-b",
                         fields=_ctx("Conformance second note", to=["z@team.example"])),
    ]

    def norm(state: DestState) -> Any:
        return {k: {kk: vv for kk, vv in v.items() if kk != "id"} for k, v in state.data["owned"].items()}

    return Scenario(f"{dest_id} (message_mcp)", lambda: build(dest_id, cfg, denv), steps,
                    lambda d, s: [], norm)


# --- entry points for the CLI ----------------------------------------------------------


def builtin_config(adapter: str) -> DestConfig:
    base: dict[str, Any] = {"adapter": adapter}
    if adapter == "markdown_tasks":
        base.update({"path": "tasks.md", "section": "## Inbox", "line_template": "- [ ] {title}{?due} [due:: {due}]{/due}", "sections": {"inbox": "## Inbox", "later": "## Later"},
                     "carry_field": "carry",
                     "quality": {"imperative": {"deny": ["fyi", "maybe"], "default_verb": "Follow up on"},
                                 "max_length": 140, "require": ["due"], "evidence": "required"}})
    elif adapter == "markdown_agenda":
        base.update({"path": "agendas", "section": "## Next", "capacity": 6, "visibility": "shared"})
    elif adapter == "journal":
        base.update({"path": "journal.md"})
    elif adapter == "calendar_file":
        base.update({"path": "calendar.ics", "visibility": "shared", "redact": {"people": True}})
    elif adapter == "calendar_mcp":
        base.update({"tools": MCP_CAL_TOOLS, "visibility": "shared"})
    elif adapter == "message_mcp":
        base.update({"tools": MCP_DRAFT_TOOLS})
    return DestConfig.model_validate(base)


def scenario_for(adapter: str, cfg: DestConfig, scratch: Path, dest_id: str, *, profile: Any = None,
                 env: Any = None, root: Path | None = None, log_dir: Path | None = None) -> Scenario:
    if adapter == "markdown_tasks":
        return tasks_scenario(cfg, scratch, dest_id, profile)
    if adapter == "markdown_agenda":
        return agenda_scenario(cfg, scratch, dest_id, profile)
    if adapter == "journal":
        return journal_scenario(cfg, scratch, dest_id)
    if adapter == "calendar_file":
        return ics_scenario(cfg, scratch, dest_id, profile)
    if adapter == "calendar_exec":
        return exec_scenario(cfg, root or scratch, dest_id, env=env, profile=profile, log_dir=log_dir)
    if adapter == "calendar_mcp":
        return mcp_calendar_scenario(cfg.model_copy(update={"tools": MCP_CAL_TOOLS}), dest_id)
    if adapter == "message_mcp":
        return mcp_message_scenario(cfg.model_copy(update={"tools": MCP_DRAFT_TOOLS}), dest_id)
    raise InputError("plugin.unknown", f"No conformance scenario for adapter {adapter!r}.", hint="docket plugin list")


def check(adapter: str, cfg: DestConfig, scratch: Path, dest_id: str, **kw: Any) -> Report:
    return run(scenario_for(adapter, cfg, scratch, dest_id, **kw))
