"""Calendar adapters: ownership, redaction on a shared calendar, ICS preservation, exec protocol, MCP gating."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from docket.cli.errors import ConfigError, DocketError, IntegrityError
from docket.config.models import Destination as DestConfig
from docket.config.models import Profile
from docket.destinations import build
from docket.destinations.base import Action, DestEnv, Operation
from docket.destinations.calendar import Marker, ownership_key
from docket.destinations.conformance import MCP_CAL_TOOLS, UNOWNED_ICS, FakeMcp

STUB = Path(__file__).resolve().parents[2] / "fixtures" / "bin" / "calbridge_stub.py"
PROFILE = Profile.model_validate({
    "me": {"name": "Robin Vale"},
    "people": [{"id": "p1", "name": "Alex Doe"}],
    "entities": [{"id": "e1", "aliases": ["Contoso Ltd"], "protected": True}],
})
SLOT = {"start": "2030-02-04T15:00:00Z", "end": "2030-02-04T16:00:00Z"}


def ics(tmp_path, **cfg):
    (tmp_path / "cal.ics").write_bytes(UNOWNED_ICS.encode())
    base = {"adapter": "calendar_file", "path": "cal.ics", "visibility": "shared", "redact": {"people": True}}
    base.update(cfg)
    return build("cal", DestConfig.model_validate(base), DestEnv(root=tmp_path, profile=PROFILE))


def place(title, seed="s1", **kw):
    return Action(kind="calendar.place", destination="cal", key=seed, fields={"title": title, "note": "", **SLOT, **kw})


def test_marker_template_renders_and_parses():
    m = Marker("(owned by docket {destination} {key})", "cal")
    notes = m.attach("Some notes", "dk_123abc")
    assert notes == "Some notes\n\n(owned by docket cal dk_123abc)"
    assert m.find(notes) == "dk_123abc" and m.find("(owned by docket other dk_123abc)") is None
    with pytest.raises(ConfigError):
        Marker("no key here", "cal")


def test_only_marker_carrying_events_without_attendees_or_recurrence_are_owned(tmp_path):
    d = ics(tmp_path)
    st = d.snapshot()
    assert st.data["owned"] == {}
    assert st.data["unowned_ids"] == ["meeting-1@conformance", "series-1@conformance"]
    assert [b[2] for b in st.data["busy"]] == ["meeting-1@conformance", "series-1@conformance"]
    assert "attendees" not in Operation.model_fields and "recurrence" not in Operation.model_fields


def test_create_move_delete_keep_unowned_events_byte_identical(tmp_path):
    d = ics(tmp_path)
    [op] = d.preview(place("Focus on the review"), d.snapshot())
    assert op.verb == "create" and op.key == ownership_key("cal", "s1")
    rec = d.apply(op)
    assert rec.outcome == "applied" and d.verify(op, rec).ok
    assert d.apply(op).outcome == "already_present"
    text = (tmp_path / "cal.ics").read_bytes().decode()
    assert "[docket:" + op.key + "]" in text
    unowned_part = UNOWNED_ICS.split("END:VCALENDAR")[0]
    assert text.startswith(unowned_part)
    moved = d.preview(place("Focus on the review", start="2030-02-05T09:00:00Z", end="2030-02-05T10:00:00Z"),
                      d.snapshot())
    assert [o.verb for o in moved] == ["move"]
    d.apply(moved[0])
    [rel] = d.preview(Action(kind="calendar.release", destination="cal", key="s1"), d.snapshot())
    d.apply(rel)
    assert d.apply(rel).outcome == "already_present"
    assert (tmp_path / "cal.ics").read_bytes().decode() == UNOWNED_ICS


def test_unowned_targets_are_refused(tmp_path):
    d = ics(tmp_path)
    with pytest.raises(IntegrityError) as e:
        d.preview(Action(kind="calendar.release", destination="cal", target="meeting-1@conformance"), d.snapshot())
    assert e.value.code == "dest.unowned"
    forged = Operation(op_id="op_x", destination="cal", verb="delete", key="dk_aaaaaaaaaaaa",
                       external_id="series-1@conformance")
    with pytest.raises(IntegrityError) as e:
        d.apply(forged)  # carries a marker but recurs: never owned
    assert e.value.code == "dest.unowned"
    assert (tmp_path / "cal.ics").read_bytes().decode() == UNOWNED_ICS


def test_shared_calendar_redacts_and_refuses_a_protected_entity_leak(tmp_path):
    d = ics(tmp_path, title_template="{title}", notes_template="{text}")
    [op] = d.preview(place("Prep for Contoso Ltd with Alex Doe", text="Contoso Ltd asked again"), d.snapshot())
    assert op.title == "Prep for [redacted] with [person]" and op.notes == "[redacted] asked again"
    with pytest.raises(IntegrityError) as e:
        d.preview(place("Prep the ContosoLtd renewal"), d.snapshot())
    assert e.value.code == "dest.redaction_leak" and e.value.exit_code == 4
    assert e.value.details["field"] == "destinations.cal.title"
    with pytest.raises(IntegrityError):
        d.preview(place("Fine title", text="notes about contoso-ltd"), d.snapshot().model_copy())
    assert (tmp_path / "cal.ics").read_bytes().decode() == UNOWNED_ICS  # nothing was written


def test_sensitivity_class_renders_the_class_template(tmp_path):
    d = ics(tmp_path, redact={"classes": {"personal": "{area} time"}})
    [op] = d.preview(place("Talk to Alex Doe about the review", area="Team", fields={"sensitivity": "personal"}),
                     d.snapshot())
    assert op.title == "Team time" and op.notes == ""


def test_private_calendar_keeps_full_detail(tmp_path):
    d = ics(tmp_path, visibility="private")
    [op] = d.preview(place("Prep for Contoso Ltd"), d.snapshot())
    assert op.title == "Prep for Contoso Ltd"


def test_mcp_calendar_writes_only_declared_tools():
    srv = FakeMcp()
    read_only = build("cal", DestConfig.model_validate({"adapter": "calendar_mcp", "tools": {"list": "list_events"}}),
                      DestEnv(root=Path("."), profile=PROFILE, call_tool=lambda _id: srv))
    assert read_only.capabilities == frozenset()
    assert not read_only.can_accept(place("x")).ok
    full = build("cal", DestConfig.model_validate({"adapter": "calendar_mcp", "tools": MCP_CAL_TOOLS}),
                 DestEnv(root=Path("."), profile=PROFILE, call_tool=lambda _id: srv))
    [op] = full.preview(place("x"), full.snapshot())
    assert full.apply(op).outcome == "applied" and srv.calls[-1] == "create_event"


def test_mcp_without_a_transport_is_unavailable_not_a_crash():
    d = build("cal", DestConfig.model_validate({"adapter": "calendar_mcp", "tools": MCP_CAL_TOOLS}),
              DestEnv(root=Path("."), profile=PROFILE))
    with pytest.raises(DocketError) as e:
        d.snapshot()
    assert e.value.code == "dest.unavailable" and e.value.kind == "external"


def test_message_drafts_never_send():
    from docket.destinations.conformance import MCP_DRAFT_TOOLS
    srv = FakeMcp()
    d = build("mail", DestConfig.model_validate({"adapter": "message_mcp", "tools": MCP_DRAFT_TOOLS}),
              DestEnv(root=Path("."), profile=PROFILE, call_tool=lambda _id: srv))
    assert d.verbs == frozenset({"draft"}) and d.capabilities == frozenset({"message.draft"})
    [op] = d.preview(Action(kind="message.draft", destination="mail", key="m1",
                            fields={"title": "Hello", "note": "Body", "to": ["x@team.example"]}), d.snapshot())
    d.apply(op)
    assert set(srv.calls) == {"list_drafts", "create_draft"} and srv.calls.count("create_draft") == 1


# --- exec protocol (subprocess: the stub bridge) ----------------------------------------


def exec_dest(tmp_path, *flags, timeout=10.0):
    cfg = DestConfig.model_validate({"adapter": "calendar_exec", "calendar": "work", "timeout_s": timeout,
                                     "program": [sys.executable, str(STUB), "--state", str(tmp_path / "s.json"),
                                                 *flags]})
    return build("cal", cfg, DestEnv(root=tmp_path, profile=PROFILE, log_dir=tmp_path / "logs"))


def test_exec_round_trip_and_generic_twin(tmp_path):
    d = exec_dest(tmp_path, "--no-sandbox")
    try:
        assert d.backend.client.hello["name"] == "calbridge-stub"
        [op] = d.preview(place("Deep focus"), d.snapshot())
        rec = d.apply(op)
        assert rec.outcome == "applied" and rec.external_id == "ev-1" and d.verify(op, rec).ok
        twin = d.sandbox()
        [op2] = twin.preview(place("Twin block", seed="s2"), twin.snapshot())
        twin.apply(op2)
        assert set(twin.snapshot().data["owned"]) == {op.key, op2.key}
        assert set(d.snapshot().data["owned"]) == {op.key}  # the live program never saw the twin's write
        assert d.backend.status() == {"access": "granted"}
        assert d.backend.mirror({"from": "2030-02-01", "to": "2030-02-02"}) == {"planned": []}
    finally:
        d.close()


def test_exec_errors_keep_the_raw_message(tmp_path):
    d = exec_dest(tmp_path, "--fail", "create")
    try:
        [op] = d.preview(place("Blocked"), d.snapshot())
        with pytest.raises(DocketError) as e:
            d.apply(op)
        assert e.value.code == "dest.apply_failed" and e.value.retryable
        assert e.value.details["error"] == {"code": "permission", "message": "stub refuses create", "retryable": True}
    finally:
        d.close()


def test_exec_program_that_hangs_times_out(tmp_path):
    cfg = DestConfig.model_validate({"adapter": "calendar_exec", "timeout_s": 0.5,
                                     "program": [sys.executable, "-c", "import time; time.sleep(30)"]})
    with pytest.raises(DocketError) as e:
        build("cal", cfg, DestEnv(root=tmp_path, profile=PROFILE))
    assert e.value.code == "dest.unavailable" and "no response after" in e.value.message


def test_exec_program_environment_is_scrubbed(tmp_path):
    from docket.destinations.exec_client import ExecClient
    c = ExecClient(["true"], cwd=tmp_path, env={"PATH": "/bin", "SOME_TOKEN": "x", "DECLARED": "y"},
                   declared_env=["DECLARED"])
    assert c.env == {"PATH": "/bin", "DECLARED": "y", "DOCKET_NONINTERACTIVE": "1"}
