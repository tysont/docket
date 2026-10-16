from __future__ import annotations

from datetime import date

import pytest

from docket.cli.errors import DocketError
from docket.text.dates import normalize_date, parse_instant, parse_window

pytestmark = pytest.mark.smoke
REF = date(2026, 10, 15)  # a Thursday


@pytest.mark.parametrize("text,expected", [
    ("today", "2026-10-15"), ("tomorrow", "2026-10-16"), ("2026-11-02", "2026-11-02"),
    ("thu", "2026-10-22"),  # strictly after the reference date
    ("Thursday", "2026-10-22"), ("fri", "2026-10-16"), ("monday", "2026-10-19"), ("Mon", "2026-10-19"),
    ("wed", "2026-10-21"), ("next tue", "2026-10-20"), ("sun", "2026-10-18"), ("tues", "2026-10-20"),
])
def test_dates(text, expected):
    assert normalize_date(text, REF) == expected


@pytest.mark.parametrize("bad", ["", "someday", "2026-13-01", "10/16", "thursdays"])
def test_bad_dates(bad):
    with pytest.raises(DocketError) as ei:
        normalize_date(bad, REF)
    assert ei.value.code == "input.bad_date"


@pytest.mark.parametrize("text,expected", [
    ("thu 13:00-17:00", "2026-10-22 13:00-17:00"), ("fri 9-11", "2026-10-16 09:00-11:00"),
    ("2026-10-20 1pm-3pm", "2026-10-20 13:00-15:00"), ("tomorrow", "2026-10-16"),
    ("this week", "2026-10-15..2026-10-18"), ("THU 13:00 - 17:00", "2026-10-22 13:00-17:00"),
])
def test_windows(text, expected):
    assert parse_window(text, REF).normalized() == expected


@pytest.mark.parametrize("bad", ["thu 17:00-13:00", "thu noon", "later", "thu 13:00"])
def test_bad_windows(bad):
    with pytest.raises(DocketError) as ei:
        parse_window(bad, REF)
    assert ei.value.code == "input.bad_window"


def test_instant_needs_offset():
    assert parse_instant("2026-10-15T18:00:00-05:00").utcoffset() is not None
    with pytest.raises(DocketError):
        parse_instant("2026-10-15T18:00:00")
