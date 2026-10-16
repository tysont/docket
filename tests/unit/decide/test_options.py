from __future__ import annotations

import pytest

from docket.cli.errors import DocketError
from docket.decide.options import resolve_option

pytestmark = pytest.mark.smoke
OPTS = [
    {"id": "task", "label": "Add to backlog (Mon)", "destination": "backlog"},
    {"id": "agenda", "label": "Raise at Platform Sync", "destination": "agendas"},
    {"id": "block", "label": "Block 1h this week"},
    {"id": "drop", "label": "Dismiss"},
    {"id": "draft", "label": "Draft a reply"},
]


@pytest.mark.parametrize("text,expected", [
    ("task", "task"), ("TASK", "task"), ("2", "agenda"), ("Dismiss", "drop"), ("raise at", "agenda"),
    ("add to backlog", "task"), ("Add to backlog (Mon)", "task"), ("agendas", "agenda"), ("block 1h", "block"),
    ("draft a", "draft"), ("dism", "drop"),
])
def test_resolves(text, expected):
    assert resolve_option(text, OPTS)["id"] == expected


def test_ambiguous_lists_candidates():
    with pytest.raises(DocketError) as ei:
        resolve_option("dr", OPTS, question_id="q_1")
    assert ei.value.code == "input.ambiguous_option"
    assert {c["id"] for c in ei.value.details["candidates"]} == {"drop", "draft"}


@pytest.mark.parametrize("text", ["nothing", "9", "", "0"])
def test_unknown(text):
    with pytest.raises(DocketError) as ei:
        resolve_option(text, OPTS)
    assert ei.value.code == "input.unknown_option"
