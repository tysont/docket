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
    id: str
    label: str
    position: int
    action: str | None = None
    answer: Command


class AskQuestion(BaseModel):
    id: str
    kind: str
    prompt: str
    options: list[AskOption]
    details: dict[str, Any] = {}


class AskStep(BaseModel):
    kind: Literal["ask"] = "ask"
    stage: str
    questions: list[AskQuestion]
    answer: str
    burst: int = 1
    why: str


class WaitStep(BaseModel):
    kind: Literal["wait"] = "wait"
    stage: str | None = None
    reason: str
    instructions: str
    resume_when: str | None = None
    command: Command | None = None


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
