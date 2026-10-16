from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from docket.config import contexts
from docket.engine.stages import JOURNAL_TEMPLATE
from docket.text.templates import TemplateError, parse, render, validate

pytestmark = pytest.mark.smoke

CTX = {
    "title": "  Draft pricing memo  ", "source": "mail", "n": 3, "due": "2026-10-19", "est": 90,
    "tags": ["a", "b", "c"], "forum": {"name": "Platform Sync"}, "empty": "", "none": None,
    "people": [{"name": "Kim"}, {"name": "Lee"}], "at": "2026-10-15T14:05:00+00:00", "flag": False,
}


@pytest.mark.parametrize("tpl,expected", [
    ("{source}", "mail"),
    ("{forum.name}", "Platform Sync"),
    ("{title|trim|upper}", "DRAFT PRICING MEMO"),
    ("{source|title}", "Mail"),
    ("{title|trim|lower}", "draft pricing memo"),
    ("{title|trim|truncate:8}", "Draft p\u2026"),
    ("{title|trim|truncate:80}", "Draft pricing memo"),
    ("{due|date:%a %d}", "Mon 19"),
    ("{at|date:%H:%M}", "14:05"),
    ("{est|duration}", "1h30m"),
    ("{tags|join: / }", "a / b / c"),
    ("{tags|join}", "a, b, c"),
    ("{empty|default:none}", "none"),
    ("{missing|default:n/a}", "n/a"),
    ("{title|trim|slug}", "draft-pricing-memo"),
    ("{tags|first}", "a"),
    ("{tags|count}", "3"),
    ("{title|redact}", "[redacted]"),
    ("{?due}due {due}{/due}", "due 2026-10-19"),
    ("{?empty}x{/empty}", ""),
    ("{?flag}x{/flag}", ""),
    ("{#people}[{name}]{/people}", "[Kim][Lee]"),
    ("{#tags}{.};{/tags}", "a;b;c;"),
    ("{#people}{name}@{source} {/people}", "Kim@mail Lee@mail "),
    ("{{literal}} {source}", "{literal} mail"),
    ("{missing}", ""),
    ("{none}", ""),
    ("{forum.missing.deep}", ""),
    ("{title|date:%Y}", "  Draft pricing memo  "),  # a filter that cannot apply passes through
    ("{n|truncate:1}", "3"),
])
def test_render_table(tpl, expected):
    assert render(tpl, CTX) == expected


@pytest.mark.parametrize("bad", [
    "{", "}", "{}", "{x|nope}", "{x|truncate}", "{x|truncate:abc}", "{x|upper:3}", "{?x}open",
    "{/x}", "{?x}{/y}", "{bad name}", "{#.}{/.}",
])
def test_parse_errors(bad):
    with pytest.raises(TemplateError):
        parse(bad)


def test_validate_against_context():
    fields = ["title", "forum.name", "people[]", "people[].name", "fields.*"]
    assert validate("{title} {forum.name} {fields.queue} {#people}{name}{/people}", fields) == []
    errs = validate("{titel} {forum.room} {#title}{x}{/title}", fields)
    assert [e.field_name for e in errs] == ["titel", "forum.room", "title"]
    assert validate("{x|nope}", fields)[0].pos == 0


def test_journal_template_validates():
    assert validate(JOURNAL_TEMPLATE, contexts.JOURNAL) == []


def test_filter_set_is_closed():
    from docket.text.templates import FILTERS
    assert set(FILTERS) == {"upper", "lower", "title", "trim", "truncate", "date", "duration", "join",
                            "default", "redact", "slug", "first", "count"}


values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.sampled_from(["title", "due", "x", "name"]), c, max_size=3),
    max_leaves=10,
)


@settings(max_examples=150, deadline=None)
@given(ctx=st.dictionaries(st.sampled_from(["title", "due", "est", "tags", "x"]), values, max_size=5))
def test_rendering_is_total(ctx):
    tpl = ("{title|trim|upper|truncate:5} {due|date:%a} {est|duration} {tags|join:-|default:z} {tags|first} "
           "{tags|count} {x.y.z|slug} {?title}t{/title}{#tags}<{.}>{/tags}")
    assert isinstance(render(tpl, ctx), str)
