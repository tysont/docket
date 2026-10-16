"""Configuration commands: ``init``, ``config validate|explain``, ``presets list|show``."""
from __future__ import annotations

import copy
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


INIT_BASE: dict[str, Any] = {
    "version": 1,
    "presets": [],
    "workflows": {"daily": {"stages": ["collect", "assess", "merge", "decide", "plan", "approve", "apply", "close"]}},
    "profile": {"timezone": "UTC", "workdays": ["mon", "tue", "wed", "thu", "fri"], "me": {"name": "Your Name"}},
    "sources": {},
    "destinations": {"backlog": {"adapter": "markdown_tasks", "path": "tasks.md"}},
}


def _leaves(d: Any, path: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    if isinstance(d, dict) and d:
        return [x for k, v in d.items() for x in _leaves(v, path + (str(k),))]
    return [(".".join(path), d)]


def _merge_into(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge_into(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def init_document(presets: list[str]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """The init workspace for these presets: workspace presets listed in ``presets``, each source
    preset scaffolded as a named source that extends it. Returns (doc, sources, placeholders)."""
    from docket.config.loader import load_yaml
    from docket.config.presets import PRESET_ROOT, preset_path, scaffold_of, scope_of
    doc = copy.deepcopy(INIT_BASE)
    placeholders: list[dict[str, Any]] = []
    scaffolded: list[str] = []
    for pid in presets:
        p = preset_path(pid)
        data = load_yaml(p, p.relative_to(PRESET_ROOT).as_posix(), "preset", pid).data
        scope = scope_of(data)
        sc = copy.deepcopy(scaffold_of(data))
        if scope == "workspace":
            if pid not in doc["presets"]:
                doc["presets"].append(pid)
        else:
            srcs = sc.get("sources") if isinstance(sc.get("sources"), dict) else {}
            if not srcs:
                srcs = {pid.rsplit("/", 1)[-1]: {}}
                sc["sources"] = srcs
            for sid in list(srcs):
                srcs[sid] = {"extends": pid, **(srcs[sid] or {})}
                scaffolded.append(sid)
        for path, value in _leaves(sc):
            if path and not path.endswith(".extends"):
                placeholders.append({"path": path, "value": value, "preset": pid, "scope": scope})
        _merge_into(doc, sc)
    return doc, scaffolded, placeholders


def _render_init(doc: dict[str, Any], placeholders: list[dict[str, Any]]) -> str:
    import io

    from ruamel.yaml import YAML
    head = ["# Docket workspace. Run 'docket config validate' after editing."]
    if placeholders:
        head.append("# Placeholders written by docket init; edit each before the first run:")
        head += [f"#   {p['path']}: {p['value']!r} (from {p['preset']})" for p in placeholders]
    y = YAML()
    y.default_flow_style = None
    y.width = 120
    buf = io.StringIO()
    y.dump(doc, buf)
    return "\n".join(head) + "\n" + buf.getvalue()


@command("init", "Write a minimal valid workspace into a directory. Workspace presets are listed in presets; each "
         "source preset is scaffolded as a named source that extends it, with placeholders to edit.",
         workspace=False, mutating=True,
         args=[Arg("dir", "Target directory (default: the working directory).", nargs="?", default="."),
               Arg("--preset", "Preset id (workspace or source scope, see docket presets list); repeatable.",
                   action="append")],
         effects=["writes docket.yaml into the target directory, only when the result validates"],
         examples=["docket init ./my-workspace --preset markdown/tasks-basic --json",
                   "docket init ./my-workspace --preset files/jsonl --preset options/basic --json"])
def cmd_init(ctx: Any, ns: Any) -> Result:
    import tempfile

    from docket.config import check, invalid_error
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
    doc, sources, placeholders = init_document(presets)
    text = _render_init(doc, placeholders)
    with tempfile.TemporaryDirectory(prefix="docket-init-") as tmp:
        (Path(tmp) / "docket.yaml").write_text(text, encoding="utf-8")
        ws, issues, _ = check(Path(tmp))
    if ws is None:
        raise invalid_error(issues)  # never write a workspace that does not validate
    edits = ", ".join(p["path"] for p in placeholders)
    why = (f"edit the placeholders ({edits}), then check the workspace" if placeholders
           else "check the new workspace")
    data = {"workspace": str(target), "presets": doc["presets"], "sources": sources,
            "placeholders": placeholders, "valid": True, "errors": []}
    if ctx.dry_run:
        return Result({**data, "dry_run": True, "would_write": [str(target / "docket.yaml")], "content": text},
                      f"would write {target / 'docket.yaml'}")
    target.mkdir(parents=True, exist_ok=True)
    (target / "docket.yaml").write_text(text, encoding="utf-8")
    lines = [f"wrote {target / 'docket.yaml'}"] + [f"  edit {p['path']} (placeholder {p['value']!r})"
                                                     for p in placeholders]
    return Result({**data, "written": ["docket.yaml"]}, "\n".join(lines),
                  next=(["docket", "config", "validate", "--workspace", str(target), "--json"], why))


@command("presets list", "List shipped presets with their scope: a workspace preset goes in top-level presets, "
         "a source preset in sources.<id>.extends.", workspace=False, examples=["docket presets list --json"])
def cmd_presets_list(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import load_yaml
    from docket.config.presets import PRESET_ROOT, description, preset_ids, preset_path, scope_of
    rows = []
    for pid in preset_ids():
        p = preset_path(pid)
        lay = load_yaml(p, p.relative_to(PRESET_ROOT).as_posix(), "preset", pid)
        rows.append({"id": pid, "scope": scope_of(lay.data), "description": description(p.read_text(encoding="utf-8"))})
    return Result({"presets": rows},
                  "\n".join(f"{r['id']:<32} {r['scope']:<9} {r['description']}" for r in rows))


@command("presets show", "Print a preset's YAML, its scope and the sections it defines.", workspace=False,
         args=[Arg("id", "Preset id from docket presets list.")], examples=["docket presets show markdown/tasks-basic"])
def cmd_presets_show(ctx: Any, ns: Any) -> Result:
    from docket.config.loader import closest, load_yaml
    from docket.config.presets import (
        META_KEY, PRESET_ROOT, description, preset_ids, preset_path, scaffold_of, scope_of, usage,
    )
    p = preset_path(ns.id)
    if p is None:
        raise InputError("input.unknown_preset", f"No shipped preset {ns.id!r}.", hint="docket presets list",
                         details={"suggestion": closest(ns.id, preset_ids())})
    text = p.read_text(encoding="utf-8")
    lay = load_yaml(p, p.relative_to(PRESET_ROOT).as_posix(), "preset", ns.id)
    scope = scope_of(lay.data)
    content = {k: v for k, v in lay.data.items() if k != META_KEY}
    return Result({"id": ns.id, "file": p.relative_to(PRESET_ROOT).as_posix(), "scope": scope, "use": usage(scope),
                   "description": description(text), "scaffold": scaffold_of(lay.data),
                   "sections": sorted(content), "content": content, "yaml": text}, text)
