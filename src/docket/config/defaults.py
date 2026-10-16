"""Core defaults: the first configuration layer (origin ``core``)."""
from __future__ import annotations

from typing import Any

CORE: dict[str, Any] = {
    "decisions": {
        "option_sets": {
            "default": {
                "finding": [
                    {"id": "task", "action": "task.add", "label": "Add a task: {title|truncate:60}"},
                    {"id": "dismiss", "action": "dismiss", "label": "Dismiss"},
                ],
            },
        },
        "templates": {
            "finding_prompt": "[{source}] {title}{?from} (from {from}){/from}",
        },
    },
    "tasks": {
        "concurrency": 6,
        "assess": {"max_items": 10},
        "review": {"max_items": 10},
    },
    "signals": {
        "scoring": {"floor": 0},
    },
}

DEFAULT_TASK_LINE = "- [ ] {title}{?due} (due {due}){/due}"
DEFAULT_SECTION = "## Inbox"
DEFAULT_OPTION_SET = "default"
