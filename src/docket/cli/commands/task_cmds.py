"""Task-kind commands (TD §9): ``task kinds``, plus the workspace part of ``docket schema``.

``task issue|list|show|submit`` live with the other stage commands in
``stage_cmds``. ``docket schema <kind>-input|<kind>-output`` prints the pack and
output models of every kind, using the workspace's own vocabulary and kinds
when a workspace is found.
"""
from __future__ import annotations

from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import DocketError
from docket.cli.registry import command


def _workspace_tasks(ctx: Any) -> Any:
    """The workspace's ``tasks`` configuration, or None when no workspace is found.

    An explicit ``--workspace`` that fails to load reports its error; a workspace
    found by walking up is only a default and falls back to core vocabulary.
    """
    explicit = bool(getattr(ctx.ns, "workspace", None))
    try:
        ctx.workspace_root()
        return ctx.workspace().config.tasks
    except DocketError:
        if explicit:
            raise
        return None


def workspace_task_schemas(ctx: Any) -> dict[str, Any]:
    from docket.tasks.kinds import schemas
    tasks = _workspace_tasks(ctx)
    return schemas(tasks) if tasks is not None else {}


@command("task kinds", "List the task kinds the workspace issues, with their stage and schema names.",
         examples=["docket task kinds --json"])
def cmd_task_kinds(ctx: Any, ns: Any) -> Result:
    from docket.tasks.kinds import registry
    reg = registry(ctx.workspace().config.tasks)
    rows = [{"kind": k.name, "core": k.core, "stage": k.stage, "issued": k.select is not None,
             "description": k.description, "input_schema": k.input_schema, "output_schema": k.output_schema,
             "fields": [f.name for f in k.fields], "max_items": k.max_items, "max_bytes": k.max_bytes}
            for k in reg.values()]
    text = "\n".join(f"{r['kind']:<12} {r['stage']:<7} {'issued' if r['issued'] else 'not issued':<11} "
                     f"{r['output_schema']}" for r in rows)
    return Result({"kinds": rows}, text)
