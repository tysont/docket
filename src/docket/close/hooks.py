"""Hooks (TD §13.6, §14.5): configured commands run after collect, apply and close.

A hook is an argv (each element a template over run facts only, never source
content), a working directory, a timeout and a failure policy. It runs with
stdin closed, no controlling terminal (a new session), a scrubbed environment
(process basics, the variables the hook declares by name, the run facts as
``DOCKET_*`` and ``DOCKET_NONINTERACTIVE=1``) and its output appended to
``logs/hooks/<event>-<id>.log`` in the run directory.

Each run of a hook writes a step receipt ``hook:<event>[:<scope>]:<id>``. A hook
that succeeded, or failed under ``warn``, is never run again for the same run
and event; one that failed under ``fail`` is retried by the next attempt and
stops the stage until it succeeds.

Sandbox runs and ``--dry-run`` run a hook's declared ``dry_run`` argv instead
and record nothing; a hook without one is skipped with a note.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from docket.close import receipts
from docket.text import templates
from docket.transports.base import PASS_ENV, kill_group


@dataclass
class HookRun:
    id: str
    event: str
    outcome: str                 # ok | warned | failed | skipped
    policy: str
    argv: list[str]
    exit: int | None = None
    message: str | None = None
    log: str | None = None
    elapsed_s: float | None = None
    repeated: bool = False       # True when a receipt said it already ran (not run again)
    dry_run: bool = False

    def brief(self) -> dict[str, Any]:
        """The deterministic part, for artifacts and envelopes."""
        d = asdict(self)
        d.pop("elapsed_s")
        return d


def selected(rc: Any, event: str) -> list[tuple[str, Any]]:
    """Hooks for this event in this run's workflow, in id order (or the workflow's listed order)."""
    hooks = rc.ws.config.hooks
    wf = rc.workflow
    if event in (wf.hooks or {}):
        return [(h, hooks[h]) for h in wf.hooks[event] if h in hooks]
    name = rc.workflow_name()
    return [(h, hooks[h]) for h in sorted(hooks)
            if event in hooks[h].events and (hooks[h].workflows is None or name in hooks[h].workflows)]


def arg_context(rc: Any, event: str) -> dict[str, Any]:
    r = rc.run
    return {"run": {"id": r.id, "date": r.header.clock.date, "workflow": r.header.workflow, "dir": str(r.dir),
                    "sandbox": "true" if r.header.sandbox else ""},
            "event": event, "journal": str(r.paths.journal), "workspace": str(rc.ws.root)}


def child_env(rc: Any, hook: Any, event: str) -> dict[str, str]:
    base = rc.env
    out = {k: base[k] for k in PASS_ENV if k in base}
    for name in hook.env:
        if name in base:
            out[name] = base[name]
    r = rc.run
    out.update({"DOCKET_NONINTERACTIVE": "1", "DOCKET_RUN_ID": r.id, "DOCKET_RUN_DIR": str(r.dir),
                "DOCKET_EVENT": event, "DOCKET_JOURNAL": str(r.paths.journal),
                "DOCKET_WORKSPACE": str(rc.ws.root)})
    return out


def _execute(rc: Any, hid: str, hook: Any, event: str, argv: list[str], log: Path) -> tuple[int | None, str | None, float]:
    cwd = rc.ws.root / hook.cwd if hook.cwd else rc.ws.root
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with log.open("ab") as fh:
        fh.write(f"--- {event} {hid}: {' '.join(argv)}\n".encode())
        fh.flush()
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=child_env(rc, hook, event), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return None, f"could not start {argv[0]}: {e.strerror or e}", time.monotonic() - t0
        try:
            code = proc.wait(timeout=hook.timeout_s)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            proc.wait()
            el = time.monotonic() - t0
            return None, f"timed out after {el:.1f}s (timeout {hook.timeout_s:g}s): {' '.join(argv)}", el
    el = time.monotonic() - t0
    return code, (None if code == 0 else f"exited with status {code}"), el


def step_name(event: str, hid: str, scope: str | None) -> str:
    return f"hook:{event}:{scope}:{hid}" if scope else f"hook:{event}:{hid}"


def run_event(rc: Any, event: str, *, scope: str | None = None, dry_run: bool = False) -> list[HookRun]:
    """Run the event's hooks. Raises ``hook.failed`` after recording when a ``fail`` hook fails."""
    from docket.cli.errors import ExternalError
    ctx = arg_context(rc, event)
    db = rc.run.store.db
    out: list[HookRun] = []
    preview = dry_run or rc.run.header.sandbox
    for hid, hook in selected(rc, event):
        step = step_name(event, hid, scope)
        if not preview:
            prior = receipts.done(db, rc.run.id, step)
            if prior is not None:
                out.append(HookRun(hid, event, prior["outcome"], hook.on_failure, prior.get("argv", []),
                                   prior.get("exit"), prior.get("message"), prior.get("log"), repeated=True))
                continue
        tpl = hook.dry_run if preview else hook.command
        if tpl is None:
            out.append(HookRun(hid, event, "skipped", hook.on_failure, [],
                               message="no dry_run form declared; skipped in a dry run or sandbox run",
                               dry_run=True))
            continue
        argv = [templates.render(a, ctx) for a in tpl]
        log = rc.run.dir / "logs" / "hooks" / f"{event}-{hid}.log"
        code, msg, el = _execute(rc, hid, hook, event, argv, log)
        rel = log.relative_to(rc.run.dir).as_posix()
        ok = code == 0
        outcome = "ok" if ok else ("warned" if hook.on_failure == "warn" else "failed")
        hr = HookRun(hid, event, outcome, hook.on_failure, argv, code, msg, rel, round(el, 3), dry_run=preview)
        out.append(hr)
        if preview:
            continue
        with db:
            receipts.put(db, rc.run.id, step, outcome, {"argv": argv, "exit": code, "message": msg, "log": rel,
                                                         "elapsed_s": hr.elapsed_s, "policy": hook.on_failure})
        if outcome == "failed":
            raise ExternalError("hook.failed", f"Hook {hid} ({event}) {msg}.",
                                hint=" ".join(rc.argv(*_retry(event))), retryable=True,
                                next_command=rc.argv(*_retry(event)),
                                details={"hook": hid, "event": event, "exit": code, "message": msg,
                                         "log": str(log), "argv": argv})
    return out


def _retry(event: str) -> tuple[str, ...]:
    return {"after_collect": ("collect",), "after_apply": ("apply",), "after_close": ("close",)}[event]


__all__ = ["HookRun", "run_event", "selected", "child_env"]
