"""Integrity refusals (Test Plan §5.5, TD §14.1, §15.3).

One case per integrity code in the error catalog. Each proves the check refuses with the
documented code, kind ``integrity``, exit status 4 and a hint that names the fix. Row codes
of a task submission (identity mismatch, unknown reference, evidence not verbatim, unknown
line) refuse as ``task.rows_refused`` and carry their own code on the row, so their cases
assert both. A new integrity code without a case here fails ``test_every_integrity_code_has_a_case``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from docket.cli.catalog import ERRORS, KINDS
from docket.cli.errors import DocketError
from docket.config.models import Destination as DestConfig
from docket.config.models import Profile
from docket.destinations import build
from docket.destinations.base import Action, DestEnv, make_op
from docket.store.paths import state_root
from tests.agent_loop import run_agent

PROFILE = Profile.model_validate({
    "me": {"name": "Robin Vale"}, "people": [{"id": "p1", "name": "Alex Doe"}],
    "entities": [{"id": "e1", "aliases": ["Contoso Ltd"], "protected": True}],
})
BACKLOG = "# Tasks\n\n## Inbox\n- [ ] Review the runbook [source:: seed]\n"
RERUN = "docket task submit t_x /tmp/r.json"


class Refused:
    """What a case produced: the error body, the exit status and, for a row code, the row codes."""

    def __init__(self, body: dict[str, Any], exit_code: int) -> None:
        self.body, self.exit = body, exit_code

    @classmethod
    def of(cls, e: DocketError) -> "Refused":
        return cls(e.body(), e.exit_code)

    @property
    def row_codes(self) -> set[str]:
        return {r.get("code") for r in (self.body.get("details") or {}).get("rows", [])}


def expect(fn: Callable[[], Any]) -> Refused:
    try:
        fn()
    except DocketError as e:
        return Refused.of(e)
    raise AssertionError("no refusal")


def cli_refusal(code: int, env: dict[str, Any]) -> Refused:
    assert not env["ok"], env
    return Refused(env["error"], code)


# --- destinations -------------------------------------------------------------------------


def tasks_dest(tmp_path: Path, **cfg: Any) -> Any:
    (tmp_path / "tasks.md").write_text(BACKLOG)
    base = {"adapter": "markdown_tasks", "path": "tasks.md", "section": "## Inbox",
            "line_template": "- [ ] {title} [source:: {source}]{?due} [due:: {due}]{/due}"}
    base.update(cfg)
    return build("backlog", DestConfig.model_validate(base), DestEnv(root=tmp_path, profile=PROFILE,
                                                                     sandbox_dir=tmp_path / "sbx"))


def add(title: str, **kw: Any) -> Action:
    return Action(kind="task.add", destination="backlog", question_id="q_1", option_id="task",
                  fields={"title": title, "source": "chat", "run": {"date": "2026-10-15"}, **kw})


def calendar_dest(tmp_path: Path, **cfg: Any) -> Any:
    base = {"adapter": "calendar_file", "path": "cal.ics", "calendar": "work", **cfg}
    return build("cal", DestConfig.model_validate(base), DestEnv(root=tmp_path, profile=PROFILE))


def case_line_changed(tmp_path: Path, **_: Any) -> Refused:
    d = tasks_dest(tmp_path)
    close = Action(kind="task.close", destination="backlog", target="- [ ] Review the runbook [source:: seed]")
    [op] = d.preview(close, d.snapshot())
    (tmp_path / "tasks.md").write_text(BACKLOG.replace("Review the runbook", "Review the edited runbook"))
    return expect(lambda: d.apply(op))


def case_redaction_leak(tmp_path: Path, **_: Any) -> Refused:
    d = calendar_dest(tmp_path, visibility="shared", redact={"people": True}, title_template="{title}")
    act = Action(kind="calendar.place", destination="cal", key="seed-1",
                 fields={"title": "Renewal with ContosoLtd", "start": "2030-01-08T15:00:00Z",
                         "end": "2030-01-08T16:00:00Z"})
    return expect(lambda: d.preview(act, d.snapshot()))


def case_unowned(tmp_path: Path, **_: Any) -> Refused:
    d = tasks_dest(tmp_path)
    op = make_op("backlog", "append", ident="x", path="elsewhere.md", anchor="## Inbox", line="- [ ] Stray")
    return expect(lambda: d.apply(op))


def case_state_changed(tmp_path: Path, **_: Any) -> Refused:
    d = calendar_dest(tmp_path)
    op = make_op("cal", "move", ident="dk_gone", calendar="work", key="dk_gone", external_id="nope",
                 start="2030-01-08T15:00:00Z", end="2030-01-08T16:00:00Z")
    return expect(lambda: d.apply(op))


def case_quality(tmp_path: Path, **_: Any) -> Refused:
    d = tasks_dest(tmp_path, quality={"require": ["due"]})
    return expect(lambda: d.preview(add("Plan the offsite"), d.snapshot()))


def case_not_accepted(tmp_path: Path, **_: Any) -> Refused:
    d = tasks_dest(tmp_path)
    act = Action(kind="calendar.place", destination="backlog", key="k", fields={})
    return expect(lambda: d.preview(act, d.snapshot()))


def case_unsupported(tmp_path: Path, **_: Any) -> Refused:
    d = tasks_dest(tmp_path)
    op = make_op("backlog", "create", ident="x", key="dk_x", calendar="work", start="2030-01-08T15:00:00Z",
                 end="2030-01-08T16:00:00Z")
    return expect(lambda: d.apply(op))


# --- task submissions -----------------------------------------------------------------------


def _submission(kind_name: str = "assess"):
    from docket.tasks import submit
    from tests.task_helpers import item, packs_for, persona_config, persona_kinds, raw_from_lines
    cfg = persona_config("support-lead")
    kind = persona_kinds("support-lead")[kind_name]
    items = [item(i, source="tickets") for i in range(2)]
    pack = packs_for(kind, items, cfg)[0]

    def row(it: dict[str, Any], **over: Any) -> dict[str, Any]:
        r = {"id": it["id"], "source": it["source"], "keep": True, "score": 70, "kind": "request", "owner": "me",
             "ask": "Send the invented report", "evidence": [{"line": "L1", "quote": it["lines"][0]["text"]}]}
        r.update(over)
        return r

    def run(rows: list[dict[str, Any]]) -> Any:
        return submit.validate(kind, pack, {"items": rows}, raw_text=raw_from_lines(pack), rerun=RERUN)
    return pack["items"], row, run


def case_partition(**_: Any) -> Refused:
    items, row, run = _submission()
    return expect(lambda: run([row(items[0])]))


def case_rows_refused(**_: Any) -> Refused:
    items, row, run = _submission()
    return expect(lambda: run([row(items[0], evidence=[{"line": "L1", "quote": "words nobody wrote"}]),
                               row(items[1])]))


def case_evidence_not_verbatim(**_: Any) -> Refused:
    return case_rows_refused()


def case_evidence_unknown_line(**_: Any) -> Refused:
    items, row, run = _submission()
    return expect(lambda: run([row(items[0], evidence=[{"line": "L99", "quote": items[0]["lines"][0]["text"]}]),
                               row(items[1])]))


def case_identity_mismatch(**_: Any) -> Refused:
    items, row, run = _submission()
    return expect(lambda: run([row(items[0], source="pager"), row(items[1])]))


def case_unknown_ref(**_: Any) -> Refused:
    from docket.tasks import submit
    from tests.task_helpers import item, packs_for, persona_config, persona_kinds, raw_from_lines
    kind = persona_kinds("support-lead")["review"]
    items = [item(i, source="tickets", keep=True, kind="request", owner="me", ask="Send it",
                  evidence=[{"line": "L1", "quote": "x"}]) for i in range(2)]
    pack = packs_for(kind, items, persona_config("support-lead"))[0]
    rows = [{"id": pack["items"][0]["id"], "verdict": "merge", "merge_with": "item999"},
            {"id": pack["items"][1]["id"], "verdict": "ask", "against": "It may be handled."}]
    return expect(lambda: submit.validate(kind, pack, {"items": rows}, raw_text=raw_from_lines(pack), rerun=RERUN))


# --- receipts, plans, cursors -------------------------------------------------------------------


def case_receipt_conflict(tmp_path: Path, **_: Any) -> Refused:
    from docket.apply import receipts
    from docket.store.db import open_db
    db = open_db(tmp_path / "docket.db")
    op = {"op_id": "op_1", "destination": "backlog", "verb": "append", "line": "- [ ] A"}
    db.execute("INSERT INTO receipts (op_id, run_id, destination, outcome, external_id, verify_status, "
               "payload_digest, created_at) VALUES (?,?,?,?,?,?,?,?)",
               ("op_1", "r1", "backlog", "applied", None, "verified", receipts.payload_digest(op), "t"))
    try:
        return expect(lambda: receipts.existing(db, {**op, "line": "- [ ] B"}))
    finally:
        db.close()


def case_plan_invalid(persona: Any, cli: Any, pinned_now: str, monkeypatch: Any, **_: Any) -> Refused:
    from docket.plan import build as plan_build
    ws = persona("reference-like")
    cli("run", "start", "week-plan", "--now", pinned_now, "--workspace", str(ws))
    run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "plan")
    monkeypatch.setattr(plan_build, "validate", lambda res: ["block dk_t_x: injected violation"])
    return cli_refusal(*cli("plan", "build", "--workspace", str(ws)))


def _to_approve(persona: Any, cli: Any, pinned_now: str) -> tuple[Path, dict[str, Any]]:
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    return ws, res.final["questions"][0]


def case_digest_mismatch(persona: Any, cli: Any, pinned_now: str, **_: Any) -> Refused:
    ws, q = _to_approve(persona, cli, pinned_now)
    return cli_refusal(*cli("ask", "answer", "approve", "yes", "--digest", "0" * 64, "--workspace", str(ws)))


def case_stale_inputs(persona: Any, cli: Any, pinned_now: str, **_: Any) -> Refused:
    ws, q = _to_approve(persona, cli, pinned_now)
    store = ws / "worklog" / "todo.md"  # the task store changes between build and approval
    store.parent.mkdir(exist_ok=True)
    store.write_text((store.read_text() if store.exists() else "## Today\n")
                     + "TODO Someone added a line | queue=tier1 | prio=P3\n")
    return cli_refusal(*cli("ask", "answer", "approve", "yes", "--digest", q["details"]["digest"],
                            "--workspace", str(ws)))


def case_stale_artifact(persona: Any, cli: Any, pinned_now: str, **_: Any) -> Refused:
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "dispatch")
    t = res.final["tasks"][0]
    pack = json.loads(Path(t["pack_path"]).read_text())
    pack["items"] = pack["items"][:1]  # someone trimmed the pack after it was issued
    Path(t["pack_path"]).write_text(json.dumps(pack))
    Path(t["result_path"]).write_text(json.dumps({"items": []}))
    return cli_refusal(*cli(*t["submit"]["argv"][1:]))


def case_cursor_regression(persona: Any, cli: Any, tmp_path: Path, **_: Any) -> Refused:
    ws = persona("support-lead")
    cli("run", "list", "--workspace", str(ws))  # creates the database
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    db.execute("INSERT INTO cursors (source, unit, position, coverage_end, updated_run) VALUES "
               "('tickets', '*', '2026-10-15T09:00:00Z', NULL, 'r0')")
    db.commit()
    db.close()
    f = tmp_path / "watermarks.json"
    f.write_text(json.dumps({"tickets": "2026-10-01T00:00:00Z"}))
    return cli_refusal(*cli("import", "cursors", "--from", str(f), "--workspace", str(ws)))


CASES: dict[str, tuple[Callable[..., Refused], str | None]] = {
    # code -> (case, the row code it carries when the code is task.rows_refused)
    "integrity.partition": (case_partition, None),
    "integrity.stale_artifact": (case_stale_artifact, None),
    "approve.digest_mismatch": (case_digest_mismatch, None),
    "apply.receipt_conflict": (case_receipt_conflict, None),
    "apply.stale_inputs": (case_stale_inputs, None),
    "dest.line_changed": (case_line_changed, None),
    "dest.redaction_leak": (case_redaction_leak, None),
    "dest.unowned": (case_unowned, None),
    "dest.state_changed": (case_state_changed, None),
    "dest.quality": (case_quality, None),
    "dest.not_accepted": (case_not_accepted, None),
    "dest.unsupported": (case_unsupported, None),
    "plan.invalid": (case_plan_invalid, None),
    "task.rows_refused": (case_rows_refused, None),
    "task.identity_mismatch": (case_identity_mismatch, "task.identity_mismatch"),
    "task.unknown_ref": (case_unknown_ref, "task.unknown_ref"),
    "task.evidence_not_verbatim": (case_evidence_not_verbatim, "task.evidence_not_verbatim"),
    "task.evidence_unknown_line": (case_evidence_unknown_line, "task.evidence_unknown_line"),
    "cursor.regression": (case_cursor_regression, None),
}


def test_every_integrity_code_has_a_case():
    integrity = {c for c, (kind, _, _) in ERRORS.items() if kind == "integrity"}
    assert integrity == set(CASES), {"missing": sorted(integrity - set(CASES)), "extra": sorted(set(CASES) - integrity)}


@pytest.mark.parametrize("code", sorted(CASES))
def test_integrity_code_refuses_with_code_kind_exit_and_hint(code, tmp_path, persona, cli, pinned_now, monkeypatch):
    case, row_code = CASES[code]
    got = case(tmp_path=tmp_path, persona=persona, cli=cli, pinned_now=pinned_now, monkeypatch=monkeypatch)
    expected = "task.rows_refused" if row_code else code
    assert got.body["code"] == expected, got.body
    assert got.body["kind"] == "integrity" and got.exit == KINDS["integrity"] == 4
    assert got.body["hint"] and got.body["hint"].strip(), got.body
    if row_code:
        assert row_code in got.row_codes, got.body["details"]
