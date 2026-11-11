"""Titles and notes of planned blocks. A block planned from the task store is written to
the calendar destination like any other operation, so what a shared destination must not
carry is taken out of it here too."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from docket.config.models import Destination, Planning, Profile
from docket.plan.build import _titler
from docket.plan.types import Task

pytestmark = pytest.mark.smoke
PROFILE = Profile.model_validate({
    "me": {"name": "Operator"}, "timezone": "UTC",
    "people": [{"id": "lee", "name": "Lee Park"}],
    "entities": [{"id": "acme", "aliases": ["Acme Corp"], "protected": True}]})


def rc(visibility, **redact):
    cfg = SimpleNamespace(
        profile=PROFILE,
        planning=Planning.model_validate({"calendar": {"provider": "destination", "destination": "cal"},
                                          "titles": {"task": "{title}", "batch": "{count} small tasks",
                                                     "batch_notes": "{#members}- {title}\n{/members}"}}),
        destinations={"cal": Destination.model_validate({"adapter": "calendar_file", "visibility": visibility,
                                                         "path": "cal.json", "redact": redact})})
    return SimpleNamespace(ws=SimpleNamespace(config=cfg))


def task(i, title, **kw):
    return Task(id=i, title=title, est=30, **kw)


def test_a_block_on_a_shared_calendar_names_no_person_entity_or_figure():
    a = task("a", "Send Lee Park the staffing plan for Acme Corp, about $2.5M")
    b = task("b", "Ask Lee Park about the renewal")
    batch = task("g", "", kind="batch", members=("a", "b"))
    title_of = _titler(rc("shared", people=True, figures=[r"\$\d[\d,.]*[kKmMbB]?"]), {"a": a, "b": b})
    title, notes = title_of(a)
    assert title == "Send [person] the staffing plan for [redacted], about [figure]" and notes == ""
    title, notes = title_of(batch)
    assert title == "2 small tasks" and "Lee" not in notes and notes.count("[person]") == 2


def test_a_private_calendar_keeps_the_words():
    a = task("a", "Send Lee Park the staffing plan for Acme Corp")
    assert _titler(rc("private"), {"a": a})(a)[0] == "Send Lee Park the staffing plan for Acme Corp"


def test_a_block_title_can_come_from_a_field_of_the_task_line_and_link_back_by_id():
    """A task line can carry the words meant for the calendar, and its own id. The title
    template reads the field; the notes template builds a link from the id, so the detail
    stays where only the operator can open it."""
    r = rc("shared", names={"allow": ["Review", "Follow-ups", "Storage", "Notes"]},
           private={"patterns": [r"(?i)\bstaffing\b"], "title": "Private"})
    r.ws.config.planning = Planning.model_validate({
        "calendar": {"provider": "destination", "destination": "cal"},
        "titles": {"task": "{fields.cal|default:Backlog item}", "notes": "notes://find?q={id}",
                   "batch": "Follow-ups ({count})",
                   "batch_notes": "{#members}- {fields.cal|default:Backlog item}: notes://find?q={id}\n{/members}"}})
    a = task("iaaaaaaaa", "Review the Kestrel plan. Morgan Ito wrote it", fields={"cal": "Review a Storage plan"})
    b = task("ibbbbbbbb", "Reply to Morgan Ito")                                   # no words for the calendar
    c = task("icccccccc", "Send Lee Park the staffing plan", fields={"cal": "Review a plan"})
    title_of = _titler(r, {"iaaaaaaaa": a, "ibbbbbbbb": b, "icccccccc": c})
    assert title_of(a) == ("Review a Storage plan", "notes://find?q=iaaaaaaaa")
    assert title_of(b) == ("[redacted] item", "notes://find?q=ibbbbbbbb")         # "Backlog" is not on the list here
    assert title_of(c) == ("Private", "notes://find?q=icccccccc")                  # the line itself is private
    title, notes = title_of(task("g", "", kind="batch", members=("iaaaaaaaa", "icccccccc")))
    assert title == "Follow-ups (2)"
    assert notes == "- Review a Storage plan: notes://find?q=iaaaaaaaa\n- Private: notes://find?q=icccccccc"
