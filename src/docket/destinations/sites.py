"""Template sites of destination configuration (TD §3.6). Validation and rendering share these lists."""
from __future__ import annotations

from docket.config.contexts import TASK_LINE
from docket.text.templates import FieldSet

_ITEM = ["item_id", "source", "unit", "title", "text", "from", "at", "url", "score", "fields.*"]
_ANSWER = ["question_id", "due", "est", "when", "note", "area", "run.id", "run.date"]

AGENDA_LINE = FieldSet(_ITEM + _ANSWER + ["forum.id", "forum.name"])
CALENDAR_TEXT = FieldSet(_ITEM + _ANSWER + ["start", "end", "forum.id", "forum.name"])
MESSAGE_TEXT = FieldSet(_ITEM + _ANSWER + ["to"])
JOURNAL_HEADING = FieldSet(["run.id", "run.date", "run.workflow"])
MARKER = FieldSet(["key", "destination"])
CLASS_TITLE = CALENDAR_TEXT


def line_site(adapter: str) -> FieldSet | None:
    if adapter == "markdown_tasks":
        return TASK_LINE
    if adapter == "markdown_agenda":
        return AGENDA_LINE
    return None


def text_site(adapter: str) -> FieldSet | None:
    if adapter.startswith("calendar_"):
        return CALENDAR_TEXT
    if adapter == "message_mcp":
        return MESSAGE_TEXT
    return None
