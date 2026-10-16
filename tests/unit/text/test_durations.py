from __future__ import annotations

import pytest

from docket.cli.errors import DocketError
from docket.text.durations import format_duration, normalize_duration, parse_duration

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("text,minutes,norm", [
    ("45", 45, "45m"), ("45m", 45, "45m"), ("45 min", 45, "45m"), ("1h", 60, "1h"), ("1.5h", 90, "1h30m"),
    ("1h30m", 90, "1h30m"), ("1h 30m", 90, "1h30m"), ("90 minutes", 90, "1h30m"), ("2 hours", 120, "2h"),
    ("60", 60, "1h"), ("1d", 1440, "1d"), ("7d", 10080, "7d"), (" 30M ", 30, "30m"), (45, 45, "45m"),
])
def test_durations(text, minutes, norm):
    assert parse_duration(text) == minutes
    assert normalize_duration(text) == norm


@pytest.mark.parametrize("bad", ["", "abc", "1x", "h", "1h30", "-5", "1h-30m", True])
def test_bad_durations(bad):
    with pytest.raises(DocketError) as ei:
        parse_duration(bad)
    assert ei.value.code == "input.bad_duration"
    assert ei.value.exit_code == 2


def test_format_none_is_empty():
    assert format_duration(None) == ""
