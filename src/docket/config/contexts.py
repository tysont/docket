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
    # --- M8: the closed journal context (TD §13.5) ---
    "run.timezone", "run.instant",
    "sources[].reason", "sources[].coverage_end", "sources[].generations",
    "findings[].item_id", "findings[].topic", "findings[].score", "findings[].url", "findings[].from",
    "findings[].reason",
    "questions[]", "questions[].id", "questions[].kind", "questions[].prompt", "questions[].item_id",
    "answers[].action", "answers[].item_id", "answers[].note", "answers[].due", "answers[].est",
    "answers[].when", "answers[].until", "answers[].topic",
    "operations[].verb", "operations[].title", "operations[].path", "operations[].question_id",
    "operations[].item_id", "operations[].verify_status", "operations[].error_code", "operations[].error_message",
    "not_asked[].item_id", "not_asked[].score", "not_asked[].source",
    "dropped[]", "dropped[].item_id", "dropped[].title", "dropped[].source", "dropped[].status",
    "dropped[].reason", "dropped[].topic",
    "coverage_notes[]", "coverage_notes[].note", "coverage_notes[].home", "coverage_notes[].value",
    "coverage_notes[].item_id", "coverage_notes[].source", "coverage_notes[].title",
    "coverage_notes[].home_outcome",
    "context[]", "context[].id", "context[].source", "context[].title", "context[].text", "context[].at",
    "context[].url",
    "dispositions[]", "dispositions[].topic", "dispositions[].decision", "dispositions[].item_id",
    "dispositions[].destination", "dispositions[].op_id", "dispositions[].until", "dispositions[].note",
    "dispositions[].outcome",
    "cursors.advanced[]", "cursors.held[]", "cursors.regressions[]", "cursors.regressions[].source",
    "cursors.regressions[].unit", "cursors.regressions[].proposed", "cursors.regressions[].durable",
    "counts.*", "ledgers.*",
    "approval.digest", "approval.delay", "approval.delay_minutes", "approval.approved_at", "approval.built_at",
])

# Hook argv elements (TD §13.6): run facts only, never source content (TD §16).
HOOK_ARG = FieldSet(["run.id", "run.date", "run.workflow", "run.dir", "run.sandbox", "event", "journal",
                     "workspace"])

SITES: dict[str, FieldSet] = {
    "task_line": TASK_LINE,
    "option_label": OPTION_LABEL,
    "finding_prompt": FINDING_PROMPT,
    "journal": JOURNAL,
    "hook_arg": HOOK_ARG,
}

# decisions.templates.<name> -> context
DECISION_TEMPLATE_SITES: dict[str, FieldSet] = {
    "finding_prompt": FINDING_PROMPT,
}
