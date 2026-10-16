"""Declared template contexts, one per template site (TD §3.6, CFG-05).

Validation rejects a template field its site does not declare. Renderers build
their context from the same lists, so a declared field always exists at render
time (possibly empty).
"""
from __future__ import annotations

from docket.text.templates import FieldSet

_ITEM = ["item_id", "source", "unit", "title", "text", "from", "at", "url", "score", "fields.*"]

TASK_LINE = FieldSet(_ITEM + ["question_id", "due", "est", "when", "note", "area", "run.id", "run.date"])
OPTION_LABEL = FieldSet(_ITEM + ["due", "est", "when", "forum.name", "forum.id"])
FINDING_PROMPT = FieldSet(_ITEM + ["forum.name", "forum.id"])
JOURNAL = FieldSet([
    "run.id", "run.date", "run.workflow", "run.sandbox",
    "sources[]", "sources[].id", "sources[].outcome", "sources[].records",
    "findings[]", "findings[].question_id", "findings[].title", "findings[].source",
    "answers[]", "answers[].question_id", "answers[].option", "answers[].title",
    "operations[]", "operations[].op_id", "operations[].destination", "operations[].line",
    "operations[].outcome",
    "not_asked[]", "not_asked[].title",
    "unexpected[]", "unexpected[].area", "unexpected[].detail", "unexpected[].reading",
    "unexpected[].extend",
])

SITES: dict[str, FieldSet] = {
    "task_line": TASK_LINE,
    "option_label": OPTION_LABEL,
    "finding_prompt": FINDING_PROMPT,
    "journal": JOURNAL,
}

# decisions.templates.<name> -> context
DECISION_TEMPLATE_SITES: dict[str, FieldSet] = {
    "finding_prompt": FINDING_PROMPT,
}
