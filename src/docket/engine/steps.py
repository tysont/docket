"""Step models returned by ``docket next`` (TD §5.2, PRD §6.4).

Every command in a step is concrete: the real run id, task ids and paths. A
command is given both as an argv array (for programs) and a shell string (for
people and shells).
"""
from __future__ import annotations

import shlex
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class Command(BaseModel):
    argv: list[str]
    shell: str

    @classmethod
    def of(cls, argv: list[str]) -> "Command":
        return cls(argv=list(argv), shell=shlex.join(argv))


class RunStep(BaseModel):
    kind: Literal["run"] = "run"
    node: str
    command: Command
    why: str
    expect: str
    timeout_s: int = 120


class DispatchTask(BaseModel):
    task_id: str
    kind: str
    pack_path: str
    prompt_path: str
    result_path: str
    result_schema: str
    submit: Command


class DispatchStep(BaseModel):
    kind: Literal["dispatch"] = "dispatch"
    node: str
    tasks: list[DispatchTask]
    concurrency: int
    why: str


class AskOption(BaseModel):
    """An option with its own concrete answer command (``docket next --full``)."""
    id: str
    label: str
    position: int
    action: str | None = None
    answer: Command


class AskChoice(BaseModel):
    """An option in a compact ask step: answer it with the step's template."""
    id: str
    label: str


class AskQuestion(BaseModel):
    id: str
    kind: str
    prompt: str
    options: list[Union[AskOption, AskChoice]]
    details: dict[str, Any] = {}


# The two placeholders of an ask step's templates. Every other word of a template is concrete.
QUESTION, OPTION = "{question}", "{option}"
# Details a compact question keeps: what answering needs (the approve digest and diff, the
# overrides an option requires, the previous answer of a re-asked question). The rest
# (evidence, defaults, refused options, notes) is in ``docket ask show <id>``.
# ``recommended`` is the option proposed for the question's item and ``batch`` the options whose
# items are shown together: with them an agent lays out the triage view without further calls.
# ``overflow`` marks a finding past a budget, asked after the rest; ``passed`` names the approve
# question's blocks whose planned start has gone by.
COMPACT_DETAILS = ("digest", "diff_path", "counts", "requires", "previous", "recommended", "batch", "overflow",
                   "passed")
# What a card shows of a question's subject (``docket next --full``, ``ask show``, ``ask list``).
CARD_DETAILS = ("url", "links", "source", "source_label", "from", "at", "score", "text", "reviewed")


class AskStep(BaseModel):
    """Questions to ask the operator. ``answer`` is the command template: replace
    ``{question}`` with a question id and ``{option}`` with one of its option ids.
    ``show`` prints one question's evidence, defaults and refused options.

    ``not_asked`` is what an ask node was given and does not put to the operator: ``count``,
    and in the full form one row per item (``items``). None on an approve step."""
    kind: Literal["ask"] = "ask"
    node: str
    questions: list[AskQuestion]
    answer: Command
    show: Command
    full: bool = False
    burst: int = 1
    why: str
    not_asked: dict[str, Any] | None = None

    def compact(self) -> "AskStep":
        qs = []
        for q in self.questions:
            details = {k: v for k, v in q.details.items() if k in COMPACT_DETAILS}
            if isinstance(q.details.get("operations"), int):  # the approve question's operation count
                details["operations"] = q.details["operations"]
            if isinstance(details.get("previous"), dict):
                details["previous"] = {"option": details["previous"].get("option")}
            qs.append(AskQuestion(id=q.id, kind=q.kind, prompt=q.prompt, details=details,
                                  options=[AskChoice(id=o.id, label=o.label) for o in q.options]))
        brief = {"count": self.not_asked["count"]} if self.not_asked is not None else None
        return self.model_copy(update={"questions": qs, "full": False, "not_asked": brief})


class WaitStep(BaseModel):
    kind: Literal["wait"] = "wait"
    node: str | None = None
    reason: str
    instructions: str
    ready_when: str | None = None
    command: Command | None = None
    inspect: list[Command] = []     # read-only commands that show the cause (post_apply_change)
    # apply_partial: every command the driver has (repeat, accept) and the operations not written
    commands: list[Command] = []
    operations: list[dict[str, Any]] = []


Step = Annotated[Union[RunStep, DispatchStep, AskStep, WaitStep], Field(discriminator="kind")]


class StepEnvelopeData(BaseModel):
    """``data`` of ``docket next``: every step that is ready now, and each node's status."""
    run: str | None
    steps: list[Step]
    done: bool = False
    summary: dict[str, Any] | None = None
    nodes: list[dict[str, Any]] = []
