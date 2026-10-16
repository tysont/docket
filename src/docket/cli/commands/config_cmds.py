"""Configuration commands: ``init``, ``config validate|explain``, ``presets list|show``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from docket.cli.envelope import Result
from docket.cli.errors import InputError
from docket.cli.registry import Arg, command


@command("config validate", "Validate the workspace configuration: structure, references, semantics, capabilities.",
         effects=["none (read-only)"], examples=["docket config validate --json"])
def cmd_validate(ctx: Any, ns: Any) -> Result:
    from docket.config import check, invalid_error
    ws, issues, loaded = check(ctx.workspace_root())
    if ws is None:
        raise invalid_error(issues)
    data = {
        "valid": True, "files": loaded.files, "presets": loaded.presets, "digest": ws.digest,
        "sections": ws.sections, "workflows": sorted(ws.config.workflows),
        "relationship_sets": ws.relationship_sets(),
    }
    return Result(data, f"configuration valid ({len(loaded.files)} file(s), digest {ws.digest[:12]})",
                  next=(["docket", "run", "start", sorted(ws.config.workflows)[0], "--json"], "start a run")
                  if ws.config.workflows else None)


@command("config explain", "Show a configuration value and where it came from (core, preset or workspace file).",
         args=[Arg("path", "Dotted path, for example profile.timezone or workflows.daily.stages.")],
         examples=["docket config explain profile.timezone --json", "docket config explain destinations --json"])
def cmd_explain(ctx: Any, ns: Any) -> Result:
    from docket.config import explain
    ws = ctx.workspace()
    out = explain(ws, ns.path)
    val = out["value"]
    shown = val if not isinstance(val, (dict, list)) else f"<{type(val).__name__} of {len(val)}>"
    return Result(out, f"{out['path']} = {shown}\norigin: {out['origin']}")


INIT_TEMPLATE = """\
# Docket workspace. Run 'docket config validate' after editing.
version: 1
presets: {presets}

workflows:
  daily:
    stages: [collect, assess, merge, decide, plan, approve, apply, close]

profile:
  timezone: UTC
  workdays: [mon, tue, wed, thu, fri]
  me:
    name: Your Name

sources: {{}}

destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
"""


@command("init", "Write a minimal valid workspace into a directory.", workspace=False, mutating=True,
         args=[Arg("dir", "Target directory (default: the working directory).", nargs="?", default="."),
               Arg("--preset", "Preset id to extend; repeatable.", action="append")],
         effects=["writes docket.yaml into the target directory"],
         examples=["docket init ./my-workspace --preset markdown/tasks-basic --json"])
def cmd_init(ctx: Any, ns: Any) -> Result:
    import json

    from docket.config import check
    from docket.config.loader import closest
    from docket.config.presets import preset_ids
    target = Path(ns.dir)
    if not target.is_absolute():
        target = ctx.cwd / target
    presets = ns.preset or []
    avail = preset_ids()
    for p in presets:
        if p not in avail:
            raise InputError("input.unknown_preset", f"No shipped preset {p!r}.", hint="docket presets list",
                             details={"suggestion": closest(p, avail), "presets": avail})
    if (target / "docket.yaml").exists():
        raise InputError("input.not_empty", f"{target} already has a docket.yaml.", hint="Choose another directory")
    text = INIT_TEMPLATE.format(presets=json.dumps(presets))
    if ctx.dry_run:
        return Result({"dry_run": True, "would_write": [str(target / "docket.yaml")], "content": text},
                      f"would write {target / 'docket.yaml'}")
    target.mkdir(parents=True, exist_ok=True)
    (target / "docket.yaml").write_text(text, encoding="utf-8")
    ws, issues, _ = check(target)
    return Result({"workspace": str(target), "written": ["docket.yaml"], "presets": presets,
                   "valid": ws is not None, "errors": [i.as_dict() for i in issues]},
                  f"wrote {target / 'docket.yaml'}",
                  next=(["docket", "config", "validate", "--workspace", str(target), "--json"], "check the new workspace"))


@command("presets list", "List shipped presets.", workspace=False, examples=["docket presets list --json"])
def cmd_presets_list(ctx: Any, ns: Any) -> Result:
    from docket.config.presets import preset_ids, preset_path
    rows = []
    for pid in preset_ids():
        first = preset_path(pid).read_text(encoding="utf-8").splitlines()
        desc = next((ln.lstrip("# ").strip() for ln in first if ln.startswith("#")), "")
        rows.append({"id": pid, "description": desc})
    return Result({"presets": rows}, "\n".join(f"{r['id']:<32} {r['description']}" for r in rows))


@command("presets show", "Print a preset's YAML and the sections it defines.", workspace=False,
         args=[Arg("id", "Preset id from docket presets list.")], examples=["docket presets show markdown/tasks-basic"])
def cmd_presets_show(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import closest, load_yaml
    from docket.config.presets import PRESET_ROOT, preset_ids, preset_path
    p = preset_path(ns.id)
    if p is None:
        raise InputError("input.unknown_preset", f"No shipped preset {ns.id!r}.", hint="docket presets list",
                         details={"suggestion": closest(ns.id, preset_ids())})
    text = p.read_text(encoding="utf-8")
    lay = load_yaml(p, p.relative_to(PRESET_ROOT).as_posix(), "preset", ns.id)
    return Result({"id": ns.id, "file": p.relative_to(PRESET_ROOT).as_posix(), "sections": sorted(lay.data),
                   "content": lay.data, "yaml": text}, text)
