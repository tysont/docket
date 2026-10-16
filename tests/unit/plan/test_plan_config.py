"""The planning section and classify rules validate strictly (CFG-02), and resolve to a Policy."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from docket.config import check, load_workspace
from docket.plan.providers import JsonFileProvider
from docket.plan.taskstore import parse_line, read_store
from docket.plan.types import Policy
from docket.shapes.unexpected import collecting
from tests.plan_helpers import TZ, d

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
workflows:
  week:
    stages: [collect, assess, merge, decide, plan, close]
profile:
  timezone: UTC
  me: { name: Test Person }
destinations:
  backlog: { adapter: markdown_tasks, path: tasks.md }
"""


def ws(tmp_path: Path, extra: str) -> Path:
    (tmp_path / "docket.yaml").write_text(BASE + textwrap.dedent(extra), encoding="utf-8")
    return tmp_path


def codes(root):
    _, issues, _ = check(root)
    return [(i.code, i.path, i.suggestion) for i in issues]


def test_defaults_resolve_to_a_neutral_policy(tmp_path):
    cfg = load_workspace(ws(tmp_path, "")).config
    p = Policy.from_config(cfg.planning, list(cfg.profile.workdays))
    assert (p.day_window, p.granularity, p.min_block, p.max_block, p.horizon_days) == ((540, 1020), 15, 30, 120, 14)
    assert p.lunch is None and p.deep_window is None and p.daily_cap is None and p.workdays == frozenset(range(5))
    assert cfg.planning.calendar is None


def test_policy_values_parse(tmp_path):
    cfg = load_workspace(ws(tmp_path, """\
        planning:
          policy: { day_window: "08:30-16:30", lunch: "12:00-12:45", daily_cap: 5h, move_lock: 2h,
                    workdays: [mon, tue], batch: { max_minutes: 20m, block_max: 45m } }
          mirrors: [{ from: personal, to: work }]
    """)).config
    p = Policy.from_config(cfg.planning)
    assert (p.day_window, p.lunch, p.daily_cap, p.move_lock, p.batch_max, p.batch_block_max) == \
        ((510, 990), (720, 765), 300, 120, 20, 45)
    assert p.workdays == frozenset({0, 1})
    assert cfg.planning.mirrors[0].from_ == "personal" and cfg.planning.mirrors[0].as_.title == "Busy"


@pytest.mark.parametrize("extra,code,path", [
    ("planning:\n  policy: { granularty: 15m }\n", "config.unknown_key", "planning.policy.granularty"),
    ("planning:\n  policy: { granularity: lots }\n", "config.bad_type", "planning.policy.granularity"),
    ("planning:\n  policy: { lunch: noon }\n", "config.bad_type", "planning.policy.lunch"),
    ("planning:\n  workflows: [weak]\n", "config.bad_reference", "planning.workflows[0]"),
    ("planning:\n  tasks: { destination: nowhere }\n", "config.bad_reference", "planning.tasks.destination"),
    ("planning:\n  calendar: { path: c.json, marker_regex: '(' }\n", "config.unknown_key", "planning.calendar.marker_regex"),
    ("planning:\n  calendar: { provider: destination, destination: nowhere }\n", "config.bad_reference",
     "planning.calendar.destination"),
    ("planning:\n  calendar: { calendar: work }\n", "config.bad_type", "planning.calendar"),
    ("planning:\n  key_prefix: 'dk.'\n", "config.bad_type", "planning.key_prefix"),
    ("profile:\n  calendar: { classify: [{ match: { title_regex: x }, kind: meetings }] }\n", "config.bad_type",
     "profile.calendar.classify[0].kind"),
    ("profile:\n  calendar: { classify: [{ match: { titel_regex: x }, kind: focus }] }\n", "config.unknown_key",
     "profile.calendar.classify[0].match.titel_regex"),
])
def test_bad_planning_configuration_is_reported(tmp_path, extra, code, path):
    text = BASE + extra
    if extra.startswith("profile:"):
        text = text.replace("profile:\n  timezone: UTC\n  me: { name: Test Person }\n", "")
        text += "  timezone: UTC\n  me: { name: Test Person }\n"
    (tmp_path / "docket.yaml").write_text(text, encoding="utf-8")
    got = codes(tmp_path)
    assert any(c == code and p == path for c, p, _ in got), got


def test_unknown_key_suggests_the_closest(tmp_path):
    got = codes(ws(tmp_path, "planning:\n  policy: { granularty: 15m }\n"))
    assert ("config.unknown_key", "planning.policy.granularty", "granularity") in got


def test_json_provider_reads_naive_times_markers_and_skips_malformed(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"timezone": "America/Chicago", "events": [
        {"id": "a", "title": "Sync", "start": "2026-10-16T10:00:00", "end": "2026-10-16T11:00:00", "room": "B2"},
        {"id": "b", "title": "Block", "start": "2026-10-16T12:00:00Z", "end": "2026-10-16T13:00:00Z",
         "notes": "planned [key:dk_t_123456789abc]"},
        {"id": "c", "title": "Broken", "start": "not a time", "end": "x"},
        "not an object",
    ]}))
    from docket.destinations.calendar import Marker, owned_key
    marker = Marker("[key:{key}]", "calendar")  # the destination's marker is the only ownership scheme
    prov = JsonFileProvider(p, TZ, owner=lambda n, a, r: owned_key(marker, n, attendees=a, recurring=r))
    with collecting() as col:
        evs = prov.list_events(None, None)  # type: ignore[arg-type]
    assert [e.id for e in evs] == ["a", "b"]
    assert evs[0].start.utcoffset().total_seconds() == -5 * 3600 and evs[0].properties == {"room": "B2"}
    assert evs[1].owned_key == "dk_t_123456789abc"
    assert sorted(s.detail for s in col.drain()) == ["event is not an object", "malformed event: ValueError"]


def test_task_store_reads_open_lines_fields_and_sections(tmp_path):
    p = tmp_path / "tasks.md"
    p.write_text("# Tasks\n\n## Now\n- [ ] Write [[Spec|the spec]] [due:: 2026-10-20] [est:: 1h] [tier:: 2]\n"
                 "- [x] Done thing [est:: 30m]\n\n## Later\n- [ ] Someday item\n", encoding="utf-8")
    tasks, dg = read_store(p, "backlog", sections=["## Now"])
    assert [(t.title, t.fields, t.tier) for t in tasks] == [("Write the spec", {"due": "2026-10-20", "est": "1h",
                                                                                "tier": "2"}, 0)]
    assert dg != "absent" and read_store(tmp_path / "none.md", "backlog") == ([], "absent")
    a = parse_line("- [ ] Write the spec [source:: chat]", "backlog")
    b = parse_line("- [ ] Write the spec [due:: 2026-10-30]", "backlog")
    assert a and b and a[0] == b[0]          # the id survives field changes
    assert d(0).year == 2026
