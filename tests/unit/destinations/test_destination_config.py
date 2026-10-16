"""Destination configuration validation and the M6 presets (TD §3.4, §11)."""
from __future__ import annotations

import pytest

from docket.config import check, load_workspace

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
{presets}
workflows:
  daily:
    stages: [collect, assess, merge, decide, plan, approve, apply, close]
profile:
  me:
    name: Robin Vale
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
{extra}
"""


def ws(tmp_path, extra="", presets=""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docket.yaml").write_text(BASE.format(extra=extra, presets=presets))
    return tmp_path


def issues(tmp_path, extra="", presets=""):
    _, iss, _ = check(ws(tmp_path, extra, presets))
    return iss


def test_send_policy_is_not_in_v1(tmp_path):
    iss = issues(tmp_path, "  mail:\n    adapter: message_mcp\n    tools: {list: l, draft: d}\n    policy: {send: true}\n")
    hit = [i for i in iss if i.path == "destinations.mail.policy.send"]
    assert hit and hit[0].code == "config.capability_missing" and "not in v1" in hit[0].message


@pytest.mark.parametrize("extra,code,path", [
    ("  cal:\n    adapter: calendar_exec\n", "config.missing_key", "destinations.cal"),
    ("  cal:\n    adapter: calendar_file\n    marker: '[mine]'\n", "config.bad_template", "destinations.cal.marker"),
    ("  cal:\n    adapter: calendar_file\n    marker: '[{nope}:{key}]'\n", "config.bad_template", "destinations.cal.marker"),
    ("  cal:\n    adapter: calendar_file\n    title_template: '{titel}'\n", "config.bad_template",
     "destinations.cal.title_template"),
    ("  cal:\n    adapter: calendar_file\n    redact: {figures: ['(']}\n", "config.bad_regex",
     "destinations.cal.redact.figures[0]"),
    ("  t2:\n    adapter: markdown_tasks\n    grammar: {fields: yaml}\n", "config.bad_type",
     "destinations.t2.grammar.fields"),
    ("  t2:\n    adapter: markdown_tasks\n    grammar: {fields: delimited}\n", "config.missing_key",
     "destinations.t2.grammar"),
    ("  ag:\n    adapter: markdown_agenda\n    capacity: 0\n", "config.bad_type", "destinations.ag.capacity"),
    ("  ag:\n    adapter: markdown_agenda\n    line_template: '- {forum.nam}'\n", "config.bad_template",
     "destinations.ag.line_template"),
    ("  cal:\n    adapter: calendar_mcp\n    tools: {create: c}\n", "config.missing_key", "destinations.cal"),
    ("  t2:\n    adapter: markdown_tasks\n    quality: {imperative: {allow: [do]}}\n", "config.unknown_key",
     "destinations.t2.quality.imperative.allow"),
])
def test_destination_validation(tmp_path, extra, code, path):
    iss = issues(tmp_path, extra)
    assert any(i.code == code and i.path == path for i in iss), [(i.code, i.path) for i in iss]


def test_mcp_calendar_without_a_write_tool_cannot_take_placements(tmp_path):
    extra = ("  cal:\n    adapter: calendar_mcp\n    tools: {list: l}\n"
             "decisions:\n  option_sets:\n    default:\n      finding:\n"
             "        - {id: block, action: calendar.place, label: Block it, destination: cal}\n")
    assert any(i.code == "config.capability_missing" for i in issues(tmp_path, extra))
    ok = extra.replace("tools: {list: l}", "tools: {list: l, create: c}")
    assert not any(i.code == "config.capability_missing" for i in issues(tmp_path / "ok", ok))


def test_m6_presets_validate(tmp_path):
    extra = ("  agendas:\n    path: meetings\n"
             "  calendar:\n    program: [bridge]\n    calendar: work\n")
    w = load_workspace(ws(tmp_path, extra, "presets: [markdown/tasks-inline-fields, markdown/agendas, "
                                          "calendar/exec-bridge]"))
    d = w.config.destinations
    assert d["backlog"].grammar.fields == "bracket" and d["backlog"].quality.imperative.deny
    assert d["agendas"].adapter == "markdown_agenda" and d["agendas"].capacity == 6
    assert d["calendar"].adapter == "calendar_exec" and d["calendar"].visibility == "shared"
    w2 = load_workspace(ws(tmp_path / "ics", "  calendar:\n    path: cal.ics\n", "presets: [calendar/ics-file]"))
    assert w2.config.destinations["calendar"].adapter == "calendar_file"
