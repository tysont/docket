"""Line grammar round-trip tables (TD §11.3): one grammar per persona, plus the unbracketed inline style."""
from __future__ import annotations

import pytest

from docket.text.grammar import Grammar

pytestmark = pytest.mark.smoke

# reference-like: checkbox lines with bracketed inline fields
BRACKET = Grammar.from_config({"open": "- [ ] ", "done": ["- [x] ", "- [X] "], "fields": "bracket"})
# support-lead: a keyword prefix and pipe-delimited key=value fields
PIPE = Grammar.from_config({"open": "TODO ", "done": ["DONE "], "fields": "delimited",
                            "separator": " | ", "assign": "="})
INLINE = Grammar.from_config({"fields": "inline"})

BRACKET_TABLE = [
    # line, status, text, fields, links, tags
    ("- [ ] Review the runbook [source:: seed]", "open", "Review the runbook", [("source", "seed")], [], []),
    ("- [x] Ship the fix [due:: 2026-10-20] [est:: 1h]", "done", "Ship the fix",
     [("due", "2026-10-20"), ("est", "1h")], [], []),
    ("- [ ] Read [the proposal](https://docs.example/p/1) #reading", "open",
     "Read [the proposal](https://docs.example/p/1) #reading", [], ["https://docs.example/p/1"], ["reading"]),
    ("- [ ] Call back about [[Vendor Notes]] [carry:: 2]", "open", "Call back about [[Vendor Notes]]",
     [("carry", "2")], ["Vendor Notes"], []),
    ("  - [ ] Nested step", "open", "Nested step", [], [], []),
    ("- [ ] Budget review for Q4 #8", "open", "Budget review for Q4 #8", [], [], []),
    ("- [ ] Check https://status.example/x [link:: https://t.example/9]", "open", "Check https://status.example/x",
     [("link", "https://t.example/9")], ["https://status.example/x", "https://t.example/9"], []),
]

PIPE_TABLE = [
    ("TODO Call the customer back | queue=tier1 | prio=P2", "open", "Call the customer back",
     [("queue", "tier1"), ("prio", "P2")], [], []),
    ("DONE Refund issued | queue=billing | prio=P1 | due=2026-10-19", "done", "Refund issued",
     [("queue", "billing"), ("prio", "P1"), ("due", "2026-10-19")], [], []),
    ("TODO Plain task", "open", "Plain task", [], [], []),
    ("TODO Compare a | b options | prio=P3", "open", "Compare a | b options", [("prio", "P3")], [], []),
]

INLINE_TABLE = [
    ("- [ ] Draft the memo due:: 2026-10-22 est:: 45m", "open", "Draft the memo",
     [("due", "2026-10-22"), ("est", "45m")], [], []),
    ("- [ ] Nothing extra", "open", "Nothing extra", [], [], []),
]


@pytest.mark.parametrize("grammar,row", [(BRACKET, r) for r in BRACKET_TABLE] + [(PIPE, r) for r in PIPE_TABLE]
                         + [(INLINE, r) for r in INLINE_TABLE])
def test_parse_and_render_round_trip(grammar, row):
    line, status, text, fields, links, tags = row
    t = grammar.parse(line)
    assert t is not None
    assert (t.status, t.text, t.fields) == (status, text, fields)
    assert t.links == links and t.tags == tags
    assert grammar.render(t) == line


@pytest.mark.parametrize("grammar,line", [
    (BRACKET, "Some prose that is not a task"), (BRACKET, "## Inbox"), (BRACKET, ""),
    (PIPE, "- [ ] a checkbox line"), (PIPE, "NOTE not a task"),
])
def test_non_task_lines_do_not_parse(grammar, line):
    assert grammar.parse(line) is None


def test_other_checkbox_states_parse_as_other():
    t = BRACKET.parse("- [-] Cancelled thing")
    assert t is not None and t.status == "other" and BRACKET.render(t) == "- [-] Cancelled thing"


@pytest.mark.parametrize("grammar,line,key,value,expected", [
    (BRACKET, "- [ ] A [carry:: 1] [due:: x]", "carry", "2", "- [ ] A [carry:: 2] [due:: x]"),
    (BRACKET, "- [ ] A  #tag", "carry", "1", "- [ ] A  #tag [carry:: 1]"),
    (PIPE, "TODO A | queue=t1", "due", "2026-10-30", "TODO A | queue=t1 | due=2026-10-30"),
    (PIPE, "TODO A | due=2026-10-01 | queue=t1", "due", "2026-10-30", "TODO A | due=2026-10-30 | queue=t1"),
    (INLINE, "- [ ] A due:: 1", "due", "2", "- [ ] A due:: 2"),
])
def test_set_field_is_surgical(grammar, line, key, value, expected):
    assert grammar.set_field(line, key, value) == expected


def test_close_and_reopen_keep_the_rest_of_the_line():
    assert BRACKET.close("  - [ ] Do it  [due:: x]") == "  - [x] Do it  [due:: x]"
    assert PIPE.close("TODO Do it | q=1") == "DONE Do it | q=1"
    assert BRACKET.close("- [x] Done already") == "- [x] Done already"


def test_with_text_rewrites_only_the_text():
    t = BRACKET.parse("- [ ] FYI the build is red [source:: chat]")
    assert BRACKET.render(t.with_text("Follow up on the build")) == "- [ ] Follow up on the build [source:: chat]"


def test_unknown_grammar_style_is_rejected():
    with pytest.raises(ValueError):
        Grammar.from_config({"fields": "yaml"})
