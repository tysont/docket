"""Self-description: ``version``, ``schema``, ``help`` (AE-04)."""
from __future__ import annotations

import sys
from typing import Any

from docket import __version__
from docket.cli import catalog
from docket.cli.envelope import Result
from docket.cli.errors import InputError
from docket.cli.registry import REGISTRY, Arg, command


def schemas() -> dict[str, Any]:
    """Schema name -> Pydantic model or TypeAdapter, one source of truth for each shape."""
    from pydantic import TypeAdapter

    from docket.apply import receipts  # noqa: F401
    from docket.cli.envelope import Envelope, ErrorBody, UnexpectedRow
    from docket.config import models as cm
    from docket.decide.answers import Answer
    from docket.engine.stages import StageStatus
    from docket.engine.steps import Step, StepEnvelopeData
    from docket.items.records import Record
    from docket.plan.models import Operation, Plan
    from docket.store.runs import RunHeader
    from docket.tasks.kinds import registry, schemas as task_schemas

    core = registry()  # task kinds with core vocabulary; ``schema`` adds the workspace's own
    return {
        **task_schemas(),
        "assess-result": core["assess"].result_model, "review-result": core["review"].result_model,
        "envelope": Envelope, "error": ErrorBody, "unexpected": UnexpectedRow,
        "step": TypeAdapter(Step), "next-data": StepEnvelopeData, "stage-status": StageStatus,
        "config": cm.DocketConfig, "profile": cm.Profile, "workflow": cm.Workflow, "source": cm.Source,
        "destination": cm.Destination, "decisions": cm.Decisions, "option": cm.OptionConfig,
        "run-header": RunHeader, "record": Record,
        "answer": Answer, "plan": Plan, "operation": Operation,
    }


def _json_schema(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_json_schema"):
        return obj.model_json_schema()
    return obj.json_schema()


@command("version", "Print the Docket version and the schema versions it reads.", workspace=False,
         examples=["docket version --json"])
def cmd_version(ctx: Any, ns: Any) -> Result:
    from docket.engine.stages import STAGE_IMPLS
    from docket.store.db import SCHEMA_VERSION
    from docket.store.runs import RUN_SCHEMA
    data = {
        "version": __version__, "python": sys.version.split()[0],
        "schemas": {"config": 1, "database": SCHEMA_VERSION, "run": RUN_SCHEMA,
                    "stages": {k: v.schema_version for k, v in STAGE_IMPLS.items()}},
    }
    return Result(data, f"docket {__version__}")


@command("schema list", "List every schema name.", workspace=False, examples=["docket schema list --json"])
def cmd_schema_list(ctx: Any, ns: Any) -> Result:
    names = sorted(schemas())
    return Result({"schemas": names}, "\n".join(names))


@command("schema", "Print the JSON Schema of an input or output.", workspace=False,
         args=[Arg("name", "Schema name from docket schema list; <kind>-input and <kind>-output for task kinds."),
               Arg("--workspace", "Workspace whose task kinds and vocabulary to use (default: walk up from the "
                                  "working directory, then DOCKET_WORKSPACE; core defaults when none).",
                   metavar="DIR")],
         examples=["docket schema assess-output --json", "docket schema step --json"])
def cmd_schema(ctx: Any, ns: Any) -> Result:
    from docket.cli.commands.task_cmds import workspace_task_schemas
    table = {**schemas(), **workspace_task_schemas(ctx)}
    if ns.name not in table:
        from docket.config.loader import closest
        raise InputError("input.unknown_schema", f"No schema {ns.name!r}.", hint="docket schema list",
                         details={"suggestion": closest(ns.name, list(table)), "schemas": sorted(table)})
    sch = _json_schema(table[ns.name])
    import json
    return Result({"name": ns.name, "schema": sch}, json.dumps(sch, indent=2))


@command("help", "Describe commands, or list error codes with 'help errors'.", workspace=False,
         args=[Arg("topic", "A command name (words), or 'errors'.", nargs="*")],
         examples=["docket help --json", "docket help run start --json", "docket help errors --json"])
def cmd_help(ctx: Any, ns: Any) -> Result:
    topic = [t for t in (ns.topic or [])]
    if topic == ["errors"]:
        rows = catalog.as_rows()
        text = "\n".join(f"{r['code']:<28} {r['kind']:<10} exit {r['exit']:<3} {r['description']}" for r in rows)
        return Result({"errors": rows, "exit_codes": catalog.KINDS}, text)
    if topic:
        name = " ".join(topic)
        cmd = REGISTRY.get(name)
        if cmd is None:
            group = sorted(n for n in REGISTRY if n.split(" ")[0] == topic[0])
            if group:
                descs = [REGISTRY[n].describe() for n in group]
                return Result({"commands": descs}, "\n".join(f"docket {d['name']:<18} {d['summary']}" for d in descs))
            from docket.config.loader import closest
            raise InputError("input.unknown_command", f"No command {name!r}.", hint="docket help",
                             details={"suggestion": closest(name, list(REGISTRY))})
        d = cmd.describe()
        lines = [f"docket {d['name']}: {d['summary']}", "", "arguments:"]
        for a in d["arguments"]:
            lines.append(f"  {a['name']:<16} {a['help']}")
        lines += ["", "effects: " + "; ".join(d["effects"])]
        if d["examples"]:
            lines += ["examples:"] + [f"  {e}" for e in d["examples"]]
        return Result({"command": d}, "\n".join(lines))
    descs = [REGISTRY[n].describe() for n in sorted(REGISTRY)]
    text = ["docket: agent-driven triage and planning. Loop on 'docket next --json'.", "", "commands:"]
    text += [f"  {d['name']:<18} {d['summary']}" for d in descs]
    text += ["", "Every command takes --json. 'docket help <command>' for details; 'docket help errors' for codes."]
    return Result({
        "usage": "docket <command> [args] [--json] [--workspace DIR] [--run RUN] [--dry-run]",
        "loop": "Repeat: step = docket next --json; act on step.kind (run, dispatch, ask, wait, done).",
        "commands": descs,
        "exit_codes": catalog.KINDS,
    }, "\n".join(text))
