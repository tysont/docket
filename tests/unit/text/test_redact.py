"""Redaction and leak validation (TD §11.4)."""
from __future__ import annotations

import pytest

from docket.cli.errors import IntegrityError
from docket.config.models import Profile
from docket.text.redact import Redactor

pytestmark = pytest.mark.smoke

PROFILE = Profile.model_validate({
    "me": {"name": "Robin Vale"},
    "people": [{"id": "p1", "name": "Alex Doe", "aliases": ["AD"]}, {"id": "p2", "name": "Jamie Roe"}],
    "entities": [{"id": "e1", "aliases": ["Contoso Ltd"], "protected": True},
                 {"id": "e3", "aliases": ["Fabrikam"], "protected": True},
                 {"id": "e2", "aliases": ["Open Thing"], "protected": False}],
})


def make(**cfg):
    return Redactor.from_config(PROFILE, cfg)


def test_protected_entities_are_replaced_case_insensitively():
    r = make()
    assert r.redact("Prep the contoso ltd renewal") == "Prep the [redacted] renewal"
    assert r.redact("Call Fabrikam about it") == "Call [redacted] about it"
    assert r.redact("Fabrikams are fine") == "Fabrikams are fine"  # whole words only
    assert r.redact("Open Thing stays") == "Open Thing stays"  # not protected


def test_people_only_when_configured():
    assert make().redact("Ask Alex Doe") == "Ask Alex Doe"
    assert make(people=True).redact("Ask Alex Doe and AD") == "Ask [person] and [person]"
    assert make(people=True, person="a colleague").redact("Ask Jamie Roe") == "Ask a colleague"
    assert make(people=True).redact("Robin Vale owns it") == "Robin Vale owns it"  # the operator is not redacted


def test_figures_and_class_templates():
    r = make(figures=[r"\$\d[\d,.]*[kKmM]?", r"\b\d+(?:\.\d+)?%"], classes={"hr": "{area} follow-up"})
    assert r.redact("Budget is $120k, up 12%") == "Budget is [figure], up [figure]"
    ctx = {"title": "Talk to Alex about the review", "area": "Team", "fields": {"sensitivity": "hr"}}
    assert r.render_title("{title}", ctx) == "Team follow-up"
    assert r.render_title("{title}", {**ctx, "fields": {}}) == "Talk to Alex about the review"


@pytest.mark.parametrize("text", ["ContosoLtd renewal", "contoso-ltd call", "CONTOSO_LTD", "Contoso.Ltd"])
def test_leaks_catch_forms_redaction_does_not_rewrite(text):
    r = make()
    assert r.leaks(r.redact(text)), text


def test_check_refuses_with_the_leak_code():
    r = make(people=True)
    with pytest.raises(IntegrityError) as e:
        r.check("Sync with AlexDoe", where="destinations.cal.title")
    assert e.value.code == "dest.redaction_leak"
    assert e.value.details["field"] == "destinations.cal.title"
    assert "AlexDoe" not in e.value.message  # the refusal never republishes the term
    r.check("Sync with the team", where="x")  # clean text passes


def test_no_false_positive_inside_words():
    r = make(people=True)
    assert r.leaks("Roadmap ADRs") == []  # alias AD is a whole token only


def test_anything_that_looks_like_a_name_is_replaced_unless_the_workspace_allows_it():
    """The profile cannot list every person, prospect and project. Where a destination is
    read by others, a word that is written like a name and is not on the workspace's list
    of allowed words is taken out: the list says what may be shown, not what may not."""
    r = make(names={"allow": ["Review", "Draft", "Storage", "Q4", "Platform Sync"]})
    assert r.redact("Review the Storage plan with Morgan Ito") == "Review the Storage plan with [redacted]"
    assert r.redact("Draft the Q4 note for Northwind and INC-2041") == "Draft the Q4 note for [redacted] and [redacted]"
    assert r.redact("Kestrel status check") == "[redacted] status check"        # a first word gets no pass
    assert r.redact("review the plan before the platform sync") == "review the plan before the platform sync"
    assert r.redact("Review notes for the Platform Sync") == "Review notes for the Platform Sync"   # a phrase is allowed whole
    assert r.redact("Review O'Neil's doc and the McKay-Li memo") == "Review [redacted] doc and the [redacted] memo"
    assert r.redact("Review with Alex Doe") == "Review with [redacted]"          # no people rule here: still a name
    # a link is an address, not prose, and the placeholders are not names
    assert r.redact("Review https://Notes.example.org/Plan?q=Kestrel") == "Review https://Notes.example.org/Plan?q=Kestrel"
    assert make(people=True, names={"allow": ["Review"]}).redact("Review with Alex Doe") == "Review with [person]"
    assert make().redact("Review the plan with Morgan Ito") == "Review the plan with Morgan Ito"   # off unless asked for


def test_a_private_matter_gets_the_private_title_whatever_its_words():
    """Some work should not be described at all where others can read it. A workspace names
    the patterns; text that matches one, in itself or in what it was made from, is replaced
    whole. Redacting a name out of such a title would still say what the work is."""
    r = make(private={"patterns": [r"(?i)\bhead ?count\b", r"(?i)\bperformance review\b"], "title": "Private"})
    assert r.title("Send the headcount plan") == "Private"
    assert r.title("Send the plan", source="Send the plan. It covers Headcount for next year") == "Private"
    assert r.title("Review the Contoso Ltd plan") == "Review the [redacted] plan"
    assert make().title("Send the headcount plan") == "Send the headcount plan"
