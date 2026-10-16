"""Markdown adapters: quality rules, ownership, agenda capacity and preserved sections (TD §11.2, §11.3, §11.5)."""
from __future__ import annotations

import pytest

from docket.cli.errors import IntegrityError
from docket.config.models import Destination as DestConfig
from docket.config.models import Profile
from docket.destinations import build
from docket.destinations.base import Action, DestEnv
from docket.destinations.markdown_io import line_digest
from docket.shapes.unexpected import collecting

pytestmark = pytest.mark.smoke

PROFILE = Profile.model_validate({
    "me": {"name": "Robin Vale"},
    "people": [{"id": "p1", "name": "Alex Doe"}],
    "entities": [{"id": "e1", "aliases": ["Contoso Ltd"], "protected": True}],
    "forums": [{"id": "weekly", "name": "Weekly", "agenda": {"destination": "agendas", "path": "meet/weekly.md"}}],
})

BACKLOG = """# Tasks

## Inbox
- [ ] Review the runbook [source:: seed]
Some free prose that is not a task.

## Someday
- [ ] Read the proposal
"""


def tasks(tmp_path, **cfg):
    (tmp_path / "tasks.md").write_text(BACKLOG)
    base = {"adapter": "markdown_tasks", "path": "tasks.md", "section": "## Inbox",
            "sections": {"inbox": "## Inbox", "someday": "## Someday"}, "carry_field": "carry",
            "line_template": "- [ ] {title} [source:: {source}]{?due} [due:: {due}]{/due}"}
    base.update(cfg)
    return build("backlog", DestConfig.model_validate(base), DestEnv(root=tmp_path, profile=PROFILE,
                                                                     sandbox_dir=tmp_path / "sbx"))


def add(title, **kw):
    fields = {"title": title, "source": "chat", "run": {"date": "2026-10-15"}, **kw}
    return Action(kind="task.add", destination="backlog", fields=fields, question_id="q_1", option_id="task")


def preview(d, action, fix=None):
    return d.preview(action, d.snapshot(), fix=fix)


def test_append_lands_at_the_end_of_its_tier_and_is_idempotent(tmp_path):
    d = tasks(tmp_path)
    [op] = preview(d, add("Plan the offsite", tier="someday"))
    assert (op.verb, op.anchor, op.line) == ("append", "## Someday", "- [ ] Plan the offsite [source:: chat]")
    assert d.apply(op).outcome == "applied"
    assert d.apply(op).outcome == "already_present"
    assert d.verify(op, None).ok
    text = (tmp_path / "tasks.md").read_text()
    assert text.endswith("## Someday\n- [ ] Read the proposal\n- [ ] Plan the offsite [source:: chat]\n")
    assert "Some free prose that is not a task." in text


def test_op_id_is_a_function_of_destination_verb_anchor_and_payload(tmp_path):
    d = tasks(tmp_path)
    a = preview(d, add("Plan the offsite"))[0]
    b = preview(d, add("Plan the offsite").model_copy(update={"question_id": "q_other"}))[0]
    c = preview(d, add("Plan the offsite", tier="someday"))[0]
    assert a.op_id == b.op_id and a.op_id != c.op_id


def test_imperative_denylist_rewrites_and_records_a_shape(tmp_path):
    d = tasks(tmp_path, quality={"imperative": {"deny": ["fyi", "heads up"], "default_verb": "Follow up on"}})
    with collecting() as col:
        [op] = preview(d, add("FYI: the nightly job failed"))
        [op2] = preview(d, add("Heads up, the queue stalled"))
        [op3] = preview(d, add("Note the odd opening word"))  # not denied: accepted as written
    assert op.line.startswith("- [ ] Follow up on the nightly job failed")
    assert op2.line.startswith("- [ ] Follow up on the queue stalled")
    assert op3.line.startswith("- [ ] Note the odd opening word")
    shapes = col.drain()
    assert [(s.area, s.detail) for s in shapes] == [("dest.quality", "task opens with a denied word")]
    assert shapes[0].extend == "destinations.backlog.quality.imperative"


def test_required_fields_take_answer_then_defaults_then_refuse_with_the_override(tmp_path):
    d = tasks(tmp_path, quality={"require": ["due", "est", "link"], "defaults": {"est": "30m"}})
    [op] = preview(d, add("Ship it", due="2026-10-20", url="https://t.example/1"))
    assert op.line == "- [ ] Ship it [source:: chat] [due:: 2026-10-20] [est:: 30m] [link:: https://t.example/1]"
    d2 = tasks(tmp_path, quality={"require": ["due"], "defaults": {"due": "+3d"}})
    assert preview(d2, add("Ship it"))[0].line.endswith("[due:: 2026-10-18]")
    d3 = tasks(tmp_path, quality={"require": ["due"]})
    fix = lambda flag, ph: f"docket ask answer q_1 task {flag} {ph}"  # noqa: E731
    with pytest.raises(IntegrityError) as e:
        preview(d3, add("Ship it"), fix=fix)
    assert e.value.code == "dest.quality" and e.value.kind == "integrity"
    assert e.value.details["fix"] == "docket ask answer q_1 task --due <date>" == e.value.hint


def test_max_length_and_evidence(tmp_path):
    d = tasks(tmp_path, quality={"max_length": 20})
    with pytest.raises(IntegrityError) as e:
        preview(d, add("A title that is far too long for the limit"), fix=lambda f, p: f"answer {f}")
    assert e.value.details["rule"] == "max_length" and e.value.details["fix"] == "answer --title"
    d2 = tasks(tmp_path, quality={"evidence": "required"})
    with pytest.raises(IntegrityError) as e:
        preview(d2, add("Ship it"))
    assert e.value.details["rule"] == "evidence"
    assert preview(d2, add("Ship it", text="verbatim source text"))


def test_carry_close_and_update_replace_exactly_one_line(tmp_path):
    d = tasks(tmp_path)
    target = "- [ ] Review the runbook [source:: seed]"
    [carry] = d.preview(Action(kind="task.carry", destination="backlog", target=target), d.snapshot())
    assert carry.old_digest == line_digest(target) and carry.line == target + " [carry:: 1]"
    assert d.apply(carry).outcome == "applied" and d.apply(carry).outcome == "already_present"
    [again] = d.preview(Action(kind="task.carry", destination="backlog", target=carry.line), d.snapshot())
    assert again.line.endswith("[carry:: 2]")
    [close] = d.preview(Action(kind="task.close", destination="backlog", target="- [ ] Read the proposal"),
                        d.snapshot())
    assert close.anchor == "## Someday" and close.line == "- [x] Read the proposal"
    d.apply(close)
    assert "- [x] Read the proposal" in (tmp_path / "tasks.md").read_text()


def test_a_line_edited_after_preview_is_refused_with_the_current_text(tmp_path):
    d = tasks(tmp_path)
    [op] = d.preview(Action(kind="task.close", destination="backlog",
                            target="- [ ] Review the runbook [source:: seed]"), d.snapshot())
    p = tmp_path / "tasks.md"
    p.write_text(p.read_text().replace("Review the runbook", "Review the runbook today"))
    with pytest.raises(IntegrityError) as e:
        d.apply(op)
    assert e.value.code == "dest.line_changed" and e.value.details["matches"] == 0
    assert "Review the runbook today" in e.value.details["current"]
    assert "Review the runbook today" in p.read_text()  # not overwritten


def test_duplicate_matches_refuse_too(tmp_path):
    d = tasks(tmp_path)
    p = tmp_path / "tasks.md"
    p.write_text(p.read_text().replace("Some free prose", "- [ ] Review the runbook [source:: seed]\nSome free prose"))
    with pytest.raises(IntegrityError) as e:
        d.preview(Action(kind="task.close", destination="backlog", target="- [ ] Review the runbook [source:: seed]"),
                  d.snapshot())
    assert e.value.code == "dest.line_changed" and e.value.details["matches"] == 2


def test_twin_writes_a_copy_and_never_the_live_file(tmp_path):
    d = tasks(tmp_path)
    twin = d.sandbox()
    [op] = preview(twin, add("Twin only"))
    twin.apply(op)
    assert "Twin only" not in (tmp_path / "tasks.md").read_text()
    assert "Twin only" in (tmp_path / "sbx" / "backlog" / "tasks.md").read_text()


def test_shared_task_store_redacts_and_refuses_leaks(tmp_path):
    d = tasks(tmp_path, visibility="shared")
    assert preview(d, add("Call Contoso Ltd back"))[0].line.startswith("- [ ] Call [redacted] back")
    with pytest.raises(IntegrityError) as e:
        preview(d, add("Call ContosoLtd back"))
    assert e.value.code == "dest.redaction_leak"


AGENDA = """# Weekly

## Next
- Topic one
- Topic two

## 2026-10-08
- An old topic
  - with a sub-point

## Decisions log
Free text the adapter does not know.
"""


def agenda(tmp_path, capacity=3):
    (tmp_path / "meet").mkdir(exist_ok=True)
    (tmp_path / "meet" / "weekly.md").write_text(AGENDA)
    (tmp_path / "meet" / "other.md").write_text("# Other\n")
    cfg = DestConfig.model_validate({"adapter": "markdown_agenda", "path": "meet", "section": "## Next",
                                     "capacity": capacity})
    return build("agendas", cfg, DestEnv(root=tmp_path, profile=PROFILE, sandbox_dir=tmp_path / "sbx"))


def ag(title, **kw):
    return Action(kind="agenda.add", destination="agendas", fields={"title": title, **kw})


def test_agenda_appends_within_capacity_and_preserves_other_sections(tmp_path):
    d = agenda(tmp_path)
    [op] = d.preview(ag("Topic three", forum={"id": "weekly"}), d.snapshot())
    assert op.path == "meet/weekly.md" and op.line == "- Topic three"
    d.apply(op)
    text = (tmp_path / "meet" / "weekly.md").read_text()
    assert text == AGENDA.replace("- Topic two\n", "- Topic two\n- Topic three\n")


def test_agenda_refuses_when_full_with_a_reason(tmp_path):
    d = agenda(tmp_path, capacity=2)
    acc = d.can_accept(ag("Topic three", agenda="weekly"))
    assert not acc.ok and "full (2/2)" in acc.reason
    with pytest.raises(IntegrityError) as e:
        d.preview(ag("Topic three", agenda="weekly"), d.snapshot())
    assert e.value.code == "dest.not_accepted"
    assert not d.can_accept(ag("Topic", agenda="missing")).ok


def test_agenda_section_missing_is_added_and_recorded(tmp_path):
    d = agenda(tmp_path)
    [op] = d.preview(ag("First", agenda="other"), d.snapshot())
    with collecting() as col:
        d.apply(op)
    assert [s.detail for s in col.drain()] == ["anchor section missing"]
    assert (tmp_path / "meet" / "other.md").read_text() == "# Other\n\n## Next\n- First\n"


def test_journal_entry_is_written_once_and_an_edit_is_refused(tmp_path):
    cfg = DestConfig.model_validate({"adapter": "journal", "path": "log.md"})
    d = build("journal", cfg, DestEnv(root=tmp_path, profile=PROFILE))
    act = Action(kind="journal.write", destination="journal",
                 fields={"run": {"date": "2026-10-15", "id": "r1"}, "body": "- did a thing"})
    [op] = d.preview(act, d.snapshot())
    assert op.anchor == "## 2026-10-15 r1"
    assert d.apply(op).outcome == "applied" and d.apply(op).outcome == "already_present"
    p = tmp_path / "log.md"
    p.write_text(p.read_text().replace("did a thing", "did another thing"))
    with pytest.raises(IntegrityError) as e:
        d.apply(op)
    assert e.value.code == "dest.line_changed"


def test_crlf_files_keep_their_newlines(tmp_path):
    d = tasks(tmp_path)
    p = tmp_path / "tasks.md"
    p.write_bytes(BACKLOG.replace("\n", "\r\n").encode())
    [op] = preview(d, add("Plan the offsite"))
    d.apply(op)
    raw = p.read_bytes().decode()
    assert "\r\n- [ ] Plan the offsite [source:: chat]\r\n" in raw and "\n" not in raw.replace("\r\n", "")
