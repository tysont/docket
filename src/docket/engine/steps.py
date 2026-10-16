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
    stage: str
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
    stage: str
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
COMPACT_DETAILS = ("digest", "diff_path", "counts", "requires", "previous")


class AskStep(BaseModel):
    """Questions to ask the operator. ``answer`` is the command template: replace
    ``{question}`` with a question id and ``{option}`` with one of its option ids.
    ``show`` prints one question's evidence, defaults and refused options."""
    kind: Literal["ask"] = "ask"
    stage: str
    questions: list[AskQuestion]
    answer: Command
    show: Command
    full: bool = False
    burst: int = 1
    why: str

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
        return self.model_copy(update={"questions": qs, "full": False})


class WaitStep(BaseModel):
    kind: Literal["wait"] = "wait"
    stage: str | None = None
    reason: str
    instructions: str
    resume_when: str | None = None
    command: Command | None = None
    inspect: list[Command] = []     # read-only commands that show the cause (post_apply_change)
    rehearse: Command | None = None  # paused with rehearse_at_pause: rehearse now, so defects surface tonight


class DoneStep(BaseModel):
    kind: Literal["done"] = "done"
    summary: dict[str, Any]
    command: Command | None = None


Step = Annotated[Union[RunStep, DispatchStep, AskStep, WaitStep, DoneStep], Field(discriminator="kind")]


class StepEnvelopeData(BaseModel):
    """``data`` of ``docket next``."""
    run: str | None
    step: Step
    stages: list[dict[str, Any]] = []
