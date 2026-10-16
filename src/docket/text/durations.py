"""The only duration parser (TD §15.5).

Accepts ``45``, ``45m``, ``45 min``, ``1h``, ``1.5h``, ``1h30m``, ``2d`` and
spelled units (``90 minutes``, ``2 hours``). A bare number is minutes. The
normalized form is ``1h30m``, ``45m``, ``2h`` or ``1d``.
"""
from __future__ import annotations

import re

from docket.cli.errors import InputError

_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
}
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)?")


def parse_duration(text: str | int | float) -> int:
    """Return whole minutes. Raises ``input.bad_duration``."""
    if isinstance(text, bool):
        raise InputError("input.bad_duration", f"Not a duration: {text!r}")
    if isinstance(text, (int, float)):
        if text < 0:
            raise InputError("input.bad_duration", f"Negative duration: {text}")
        return int(round(text))
    s = str(text).strip().lower()
    if not s:
        raise InputError("input.bad_duration", "Empty duration", hint="Use 45, 45m, 1h, 1.5h or 1h30m")
    pos, total, parts = 0, 0.0, 0
    s_compact = s.replace(" and ", " ")
    while pos < len(s_compact):
        if s_compact[pos] in " ,":
            pos += 1
            continue
        m = _PART.match(s_compact, pos)
        if not m:
            break
        num, unit = float(m.group(1)), m.group(2)
        if unit is None:
            if parts or m.end() != len(s_compact.rstrip()):
                break
            mult = 1
        elif unit in _UNITS:
            mult = _UNITS[unit]
        else:
            break
        total += num * mult
        parts += 1
        pos = m.end()
    else:
        if parts:
            return int(round(total))
    raise InputError(
        "input.bad_duration",
        f"Not a duration: {text!r}",
        hint="Use 45, 45m, 1h, 1.5h or 1h30m",
        details={"received": str(text)},
    )


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return ""
    minutes = int(minutes)
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def normalize_duration(text: str | int | float) -> str:
    return format_duration(parse_duration(text))
