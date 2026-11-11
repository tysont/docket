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
