"""The command registry. Each command declares its arguments, effects and examples
once; argparse, ``docket help`` and ``docket help --json`` are built from it."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Callable

from docket.cli.errors import InputError


@dataclass
class Arg:
    name: str  # "workflow" (positional) or "--date"
    help: str
    required: bool = False
    nargs: str | None = None
    action: str | None = None
    default: Any = None
    choices: list[str] | None = None
    metavar: str | None = None

    @property
    def positional(self) -> bool:
        return not self.name.startswith("-")

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "help": self.help, "positional": self.positional,
                             "required": self.required or (self.positional and self.nargs not in ("?", "*"))}
        if self.choices:
            d["choices"] = self.choices
        if self.action in ("store_true",):
            d["flag"] = True
        if self.action == "append":
            d["repeatable"] = True
        if self.nargs:
            d["nargs"] = self.nargs
        return d


@dataclass
class Command:
    name: str
    summary: str
    handler: Callable[..., Any]
    args: list[Arg] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    mutating: bool = False
    workspace: bool = True
    run: bool = False
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # flag -> {schema, description, example}

    def common(self) -> list[Arg]:
        out = [Arg("--json", "Print the JSON envelope.", action="store_true")]
        if self.workspace:
            out.append(Arg("--workspace", "Workspace directory (default: walk up from the working directory, "
                                          "then DOCKET_WORKSPACE).", metavar="DIR"))
        if self.run:
            out.append(Arg("--run", "Run id (default: the most recent unsealed run).", metavar="RUN"))
        if self.mutating:
            out.append(Arg("--dry-run", "Report what the command would do and write nothing.", action="store_true"))
        return out

    def all_args(self) -> list[Arg]:
        return self.args + self.common()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "summary": self.summary, "mutating": self.mutating,
            "arguments": [a.describe() for a in self.all_args()],
            "effects": self.effects or ["none (read-only)"], "examples": self.examples,
            **({"inputs": self.inputs} if self.inputs else {}),
        }

    def parser(self) -> argparse.ArgumentParser:
        p = _Parser(prog=f"docket {self.name}", add_help=False, description=self.summary)
        for a in self.all_args():
            kw: dict[str, Any] = {"help": a.help}
            if a.action:
                kw["action"] = a.action
            if a.nargs:
                kw["nargs"] = a.nargs
            if a.default is not None:
                kw["default"] = a.default
            if a.choices:
                kw["choices"] = a.choices
            if a.metavar and not a.positional:
                kw["metavar"] = a.metavar
            if a.required and not a.positional:
                kw["required"] = True
            p.add_argument(a.name, **kw)
        return p


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # never print usage and exit
        raise InputError("input.bad_argument", f"{self.prog}: {message}",
                         hint=f"docket help {self.prog.removeprefix('docket ')}",
                         details={"usage": self.format_usage().strip()})

    def exit(self, status: int = 0, message: str | None = None) -> Any:
        raise InputError("input.bad_argument", message or f"{self.prog}: bad arguments")


REGISTRY: dict[str, Command] = {}


def command(name: str, summary: str, *, args: list[Arg] | None = None, effects: list[str] | None = None,
            examples: list[str] | None = None, mutating: bool = False, workspace: bool = True,
            run: bool = False, inputs: dict[str, dict[str, Any]] | None = None,
            ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = Command(name, summary, fn, args or [], effects or [], examples or [],
                                 mutating, workspace, run, inputs or {})
        return fn
    return deco


def resolve(words: list[str]) -> tuple[Command | None, int]:
    """Longest registered command name that prefixes ``words``; returns it and the words consumed."""
    for n in range(min(len(words), 3), 0, -1):
        name = " ".join(words[:n])
        if name in REGISTRY:
            return REGISTRY[name], n
    return None, 0
