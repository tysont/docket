"""Docket CLI entry point.

``main(argv, ...)`` runs one command in process and returns its exit status.
Tests call it directly; ``entry()`` wraps it for the console script.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Mapping

from docket.cli.envelope import Result, next_hint
from docket.cli.errors import DocketError, InputError
from docket.cli.registry import REGISTRY, resolve
from docket.shapes.unexpected import Shape, collecting

_LOADED = False


def _load_commands() -> None:
    global _LOADED
    if _LOADED:
        return
    from docket.cli.commands import config_cmds, meta, run_cmds, shape_cmds, stage_cmds  # noqa: F401
    _LOADED = True


def _split(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Leading global options, command words, and the rest."""
    lead: list[str] = []
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("--workspace", "--run") and i + 1 < len(argv):
            lead += argv[i:i + 2]
            i += 2
        else:
            lead.append(argv[i])
            i += 1
    words: list[str] = []
    for t in argv[i:]:
        if t.startswith("-"):
            break
        words.append(t)
    return lead, words, argv[i:]


def _parse(argv: list[str]) -> tuple[Any, Any]:
    lead, words, rest = _split(argv)
    helpish = any(a in ("-h", "--help") for a in argv)
    if helpish or not words:
        cmd = REGISTRY["help"]
        topic = [w for w in words if w != "help"] if helpish else []
        extra = ["--json"] if "--json" in argv else []
        return cmd, cmd.parser().parse_args(topic + extra)
    cmd, n = resolve(words)
    if cmd is None:
        from docket.config.loader import closest
        group = [name for name in REGISTRY if name.split(" ")[0] == words[0]]
        sugg = closest(" ".join(words[:2]), list(REGISTRY)) or closest(words[0], list(REGISTRY))
        raise InputError("input.unknown_command", f"No command {' '.join(words[:2])!r}.",
                         hint="docket help", details={"suggestion": sugg, "subcommands": sorted(group)})
    ns = cmd.parser().parse_args(rest[n:] + lead)
    return cmd, ns


def _crash_dir(ctx: Any, env: Mapping[str, str]) -> Path:
    root = ctx.state_root_or_none() if ctx is not None else None
    if root is None:
        try:
            from docket.store.paths import state_base
            root = state_base(env)
        except Exception:
            import tempfile
            root = Path(tempfile.gettempdir()) / "docket"
    return root / "crash"


def _persist_shapes(ctx: Any, shapes: list[Shape]) -> None:
    if not shapes or ctx is None or ctx.command is None or not ctx.command.mutating or ctx.dry_run:
        return
    if ctx._store is None:
        return
    store = ctx.store()
    stamp = datetime.now(timezone.utc).isoformat()
    run_dir = (store.runs_dir / ctx.run_id) if ctx.run_id else None
    with store.db:
        for s in shapes:
            store.db.execute(
                "INSERT INTO unexpected (run_id, command, area, detail, sample, reading, extend, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ctx.run_id, ctx.command.name, s.area, s.detail, s.sample, s.reading, s.extend, stamp))
    if run_dir is not None and run_dir.is_dir():
        with (run_dir / "unexpected.jsonl").open("a", encoding="utf-8") as fh:
            for s in shapes:
                fh.write(json.dumps(s.as_dict(), sort_keys=True, ensure_ascii=False) + "\n")


def _human(env_out: dict[str, Any], summary: str | None, out: IO[str], err: IO[str]) -> None:
    if env_out["ok"]:
        if summary:
            out.write(summary.rstrip() + "\n")
        for w in env_out["warnings"]:
            out.write(f"warning: {w['code']}: {w['message']}\n")
        if env_out["unexpected"]:
            out.write(f"unexpected shapes: {len(env_out['unexpected'])} (docket unexpected)\n")
        if env_out.get("next"):
            out.write(f"next: {env_out['next']['shell']}\n")
        return
    e = env_out["error"]
    err.write(f"error: {e['code']}: {e['message']}\n")
    for row in (e.get("details") or {}).get("errors", [])[:50]:
        if isinstance(row, dict) and "code" in row and "message" in row:
            where = row.get("file") or "<core>"
            if row.get("line"):
                where += f":{row['line']}:{row.get('column') or 1}"
            sugg = f" (did you mean {row['suggestion']!r}?)" if row.get("suggestion") else ""
            err.write(f"  {where}: {row['code']}: {row['message']}{sugg}\n")
    if e.get("hint"):
        err.write(f"hint: {e['hint']}\n")
    if env_out.get("next"):
        err.write(f"next: {env_out['next']['shell']}\n")


def main(argv: list[str], *, stdout: IO[str] | None = None, stderr: IO[str] | None = None,
         env: Mapping[str, str] | None = None, cwd: str | os.PathLike[str] | None = None,
         stdin: IO[str] | None = None) -> int:
    from docket.cli.context import Ctx

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    environ = dict(os.environ if env is None else env)
    workdir = Path(cwd if cwd is not None else os.getcwd())
    _load_commands()
    want_json = "--json" in argv
    ctx: Any = None
    summary: str | None = None
    with collecting() as col:
        ctx = Ctx(env=environ, cwd=workdir, stdin=stdin, command=None, ns=None)
        try:
            cmd, ns = _parse(list(argv))
            ctx.command, ctx.ns = cmd, ns
            want_json = bool(getattr(ns, "json", False))
            res: Result = cmd.handler(ctx, ns)
            summary = res.summary
            env_out: dict[str, Any] = {
                "ok": True, "command": cmd.name, "workspace": ctx.workspace_path, "run": ctx.run_id,
                "data": res.data, "warnings": ctx.warnings + res.warnings, "unexpected": [],
                "next": next_hint(*res.next) if res.next else None,
            }
            code = 0
        except DocketError as e:
            env_out = {
                "ok": False, "command": ctx.command.name if ctx.command else None,
                "workspace": ctx.workspace_path, "run": ctx.run_id, "error": e.body(),
                "warnings": ctx.warnings, "unexpected": [],
                "next": next_hint(e.next_command, e.hint or "retry") if e.next_command else None,
            }
            code = e.exit_code
        except Exception:
            tb = traceback.format_exc()
            path: str | None = None
            try:
                d = _crash_dir(ctx, environ)
                d.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                p = d / f"{stamp}-{os.getpid()}.txt"
                p.write_text(f"argv: {list(argv)!r}\n\n{tb}", encoding="utf-8")
                path = str(p)
            except Exception:
                path = None
            env_out = {
                "ok": False, "command": ctx.command.name if ctx and ctx.command else None,
                "workspace": ctx.workspace_path if ctx else None, "run": ctx.run_id if ctx else None,
                "error": DocketError("internal.bug", message="Docket hit an internal error; the traceback is in "
                                     "details.traceback_path.", hint="Report the traceback file",
                                     details={"traceback_path": path}).body(),
                "warnings": [], "unexpected": [], "next": None,
            }
            code = 70
        shapes = col.drain()
        env_out["unexpected"] = [s.as_dict() for s in shapes]
        try:
            _persist_shapes(ctx, shapes)
        finally:
            if ctx is not None:
                ctx.close()
    if want_json:
        out.write(json.dumps(env_out, ensure_ascii=False, sort_keys=False, default=str) + "\n")
    else:
        _human(env_out, summary, out, err)
    return code


def entry() -> None:
    sys.exit(main(sys.argv[1:]))


def run_capture(argv: list[str], **kw: Any) -> tuple[int, str, str]:
    """Run ``main`` and capture its output (for tests and tools)."""
    o, e = io.StringIO(), io.StringIO()
    code = main(argv, stdout=o, stderr=e, **kw)
    return code, o.getvalue(), e.getvalue()
