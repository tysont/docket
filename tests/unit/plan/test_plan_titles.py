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


def task(i, title, est=30, **kw):
    return Task(id=i, title=title, est=est, **kw)


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


def test_a_block_is_titled_with_the_label_written_for_its_task_and_can_link_back_by_id():
    """The label task writes the words for a shared calendar (TD §12.7). The title template
    reads them; the notes template can build a link from the task's id, so the detail stays
    where only the operator can open it. A task with no label yet takes the default."""
    r = rc("shared", people=True)
    r.ws.config.planning = Planning.model_validate({
        "calendar": {"provider": "destination", "destination": "cal"},
        "titles": {"task": "{label|default:Task}", "notes": "notes://find?q={id}", "batch": "Small tasks ({count})",
                   "batch_notes": "{#members}- {label|default:Task}: notes://find?q={id}\n{/members}"}})
    a, b = task("ia", "Review the Kestrel plan. Morgan Ito wrote it"), task("ib", "Reply to Lee Park")
    title_of = _titler(r, {"ia": a, "ib": b}, {"ia": "Review a storage plan"})
    assert title_of(a) == ("Review a storage plan", "notes://find?q=ia")
    assert title_of(b) == ("Task", "notes://find?q=ib")
    title, notes = title_of(task("g", "", kind="batch", members=("ia", "ib")))
    assert title == "Small tasks (2)" and notes == "- Review a storage plan: notes://find?q=ia\n- Task: notes://find?q=ib"
    # the exact-list backstop still holds: a label that names a profile person is refused
    from docket.cli.errors import IntegrityError
    leaky = _titler(rc("shared"), {"ia": a}, {"ia": "Review the plan for Acme Corp"})
    r2 = rc("shared"); r2.ws.config.planning = r.ws.config.planning
    assert _titler(r2, {"ia": a}, {"ia": "Review the plan for Acme Corp"})(a)[0] == "Review the plan for [redacted]"
    assert leaky(a)[0].startswith("Review the Kestrel plan")       # default template: the title, not the label


def test_a_part_of_a_split_task_is_titled_with_its_tasks_label_and_its_number():
    r = rc("shared")
    r.ws.config.planning = Planning.model_validate({
        "calendar": {"provider": "destination", "destination": "cal"},
        "titles": {"task": "{label|default:Task}{?part} ({part}/{parts}){/part}", "notes": "notes://find?q={id}"}})
    whole = task("ia", "Review the plans")
    part = task("ia#2", "Review the plans (2/3)", part_of="ia", part=2, parts=3)
    title_of = _titler(r, {"ia": whole}, {"ia": "Review roadmaps"})
    assert title_of(whole) == ("Review roadmaps", "notes://find?q=ia")
    assert title_of(part) == ("Review roadmaps (2/3)", "notes://find?q=ia")       # the link is to the task's line


def test_an_elapsed_part_is_recognised_as_a_block_of_its_task():
    from docket.decide.store import block_key
    from docket.plan.scheduler import split_long
    from docket.plan.types import Policy, task_key
    (p1, p2) = split_long([task("ia", "Review the plans", est=120)], Policy(split_over=90))
    assert task_key(p1, "dk_") == block_key("dk_", "ia#1") and task_key(p2, "dk_") == block_key("dk_", "ia#2")
