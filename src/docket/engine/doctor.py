"""``docket doctor`` (TD §15.4, §14.5, FR-ROB-06, NFR-05).

Doctor writes nothing. It checks, in order:

1. configuration validity;
2. the state root's writability and the database version (read-only open);
3. each transport a source uses: its binary, recordings file or endpoint, and
   that every credential reference resolves (by name; no value is printed);
4. each source's auth check, run unattended: stdin closed, no controlling TTY
   (a new session), a timeout, and ``DOCKET_NONINTERACTIVE=1``. A timeout is
   reported as "interactive or hung";
5. each destination's location, permissions and capability declarations, and,
   for an exec-protocol program, its platform permission status through the
   program's own non-prompting ``status`` verb;
6. each hook's command (and its dry-run form).

Network: remote endpoints are reported as configured, never contacted, unless
``probe`` is set; a localhost endpoint gets a TCP connect. Every failed check
names its fix.

A check is ``{"id", "area", "subject", "status": ok|warn|fail|skip, "detail", "fix"}``.
"""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
AUTH_TIMEOUT_CAP = 30.0


@dataclass
class Check:
    id: str
    area: str
    subject: str
    status: str            # ok | warn | fail | skip
    detail: str = ""
    fix: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_binary(cmd: str, *, root: Path, cwd: Path | None, env: Mapping[str, str]) -> str | None:
    """The executable a command names: a path (relative to its working directory) or a name on PATH."""
    if not cmd:
        return None
    if "/" in cmd or cmd.startswith("~"):
        p = Path(cmd).expanduser() if cmd.startswith("~") else Path(cmd)
        if not p.is_absolute():
            p = (cwd or root) / p
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(cmd, path=env.get("PATH", ""))


def run_unattended(argv: list[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float) -> dict[str, Any]:
    """Run a command the way Docket runs every subprocess (TD §14.5) and report how it ended."""
    from docket.transports.base import child_env
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=child_env(env, {}), cwd=str(cwd), start_new_session=True)
    except OSError as e:
        return {"ended": "not_started", "message": e.strerror or str(e), "elapsed_s": 0.0}
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
        proc.communicate()
        return {"ended": "timeout", "elapsed_s": round(time.monotonic() - t0, 2)}
    tail = (err or out or b"").decode("utf-8", "replace").strip()[-300:]
    return {"ended": "exit", "code": proc.returncode, "tail": tail, "elapsed_s": round(time.monotonic() - t0, 2)}


def _tcp(host: str, port: int, timeout: float = 2.0) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as e:
        return e.strerror or str(e)


class Doctor:
    def __init__(self, *, root: Path | None, env: Mapping[str, str], probe: bool = False) -> None:
        self.root = root
        self.env = dict(env)
        self.probe = probe
        self.checks: list[Check] = []
        self.ws: Any = None

    def add(self, *a: Any, **kw: Any) -> Check:
        c = Check(*a, **kw)
        self.checks.append(c)
        return c

    # --- runner -------------------------------------------------------------------
    def run(self) -> list[Check]:
        if self.root is None:
            self.add("config", "config", "workspace", "fail", "no docket.yaml found",
                     "docket init <dir>, or pass --workspace <dir>")
            return self.checks
        self.check_config()
        self.check_state()
        if self.ws is None:
            for area in ("transports", "auth", "destinations", "hooks"):
                self.add(f"{area}", area, "all", "skip", "configuration did not load", "docket config validate")
            return self.checks
        self.check_transports()
        self.check_auth()
        self.check_destinations()
        self.check_hooks()
        return self.checks

    # --- 1. configuration ---------------------------------------------------------
    def check_config(self) -> None:
        from docket.cli.errors import DocketError
        from docket.config import load_workspace
        try:
            self.ws = load_workspace(self.root)
        except DocketError as e:
            errs = (e.details or {}).get("errors") or []
            first = errs[0] if errs and isinstance(errs[0], dict) else {}
            where = first.get("file") or ""
            if first.get("line"):
                where += f":{first['line']}"
            detail = f"{e.code}: {e.message}" + (f" (first: {where} {first.get('message', '')})" if first else "")
            self.add("config", "config", str(self.root), "fail", detail.strip(),
                     e.hint or "docket config validate --workspace " + str(self.root))
            return
        n = len(self.ws.config.sources)
        self.add("config", "config", str(self.root), "ok",
                 f"valid: {n} source(s), {len(self.ws.config.destinations)} destination(s), "
                 f"{len(self.ws.config.workflows)} workflow(s)")

    # --- 2. state root and database ----------------------------------------------------
    def check_state(self) -> None:
        from docket.store.db import SCHEMA_VERSION
        from docket.store.paths import state_root
        try:
            root = state_root(self.root, self.env)
        except RuntimeError as e:
            self.add("state.root", "state", "state root", "fail", str(e), "set HOME or DOCKET_STATE_ROOT")
            return
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK | os.X_OK):
            self.add("state.root", "state", str(root), "fail", f"{probe} is not writable",
                     f"make {probe} writable, or set DOCKET_STATE_ROOT to a writable directory")
        else:
            self.add("state.root", "state", str(root), "ok",
                     "writable" if root.exists() else f"will be created under {probe}")
        db = root / "docket.db"
        if not db.is_file():
            self.add("state.db", "state", str(db), "ok", f"absent; created at schema {SCHEMA_VERSION} on first use")
            return
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            finally:
                conn.close()
            ver = int(row[0]) if row else 0
        except (sqlite3.Error, ValueError) as e:
            self.add("state.db", "state", str(db), "fail", f"cannot read the database: {e}",
                     "move the damaged database aside; the next run re-reads its sources")
            return
        if ver > SCHEMA_VERSION:
            self.add("state.db", "state", str(db), "fail",
                     f"schema {ver} is newer than this Docket understands ({SCHEMA_VERSION})",
                     f"upgrade Docket to a version that supports database schema {ver}")
        elif ver < SCHEMA_VERSION:
            self.add("state.db", "state", str(db), "ok", f"schema {ver}; migrates to {SCHEMA_VERSION} on the next command "
                     "that writes")
        else:
            self.add("state.db", "state", str(db), "ok", f"schema {ver}")

    # --- 3. transports ----------------------------------------------------------------
    def check_transports(self) -> None:
        from docket.transports.base import Secrets, resolve_credential
        cfg = self.ws.config
        used: dict[str, list[str]] = {}
        for sid, s in sorted(cfg.sources.items()):
            if s.transport:
                used.setdefault(s.transport, []).append(sid)
        for tid in sorted(cfg.transports):
            t = cfg.transports[tid]
            users = used.get(tid)
            subject = f"transports.{tid} ({t.kind})"
            if not users:
                self.add(f"transport.{tid}", "transports", subject, "skip", "no source uses it")
                continue
            secrets = Secrets()
            refs = [("bearer", t.bearer)] if t.bearer is not None else []
            refs += [(f"headers_from.{k}", v) for k, v in sorted(t.headers_from.items())]
            refs += [(f"env_from.{k}", v) for k, v in sorted(t.env_from.items())]
            missing = []
            for label, ref in refs:
                _, problem = resolve_credential(ref, self.env, self.ws.root, secrets)
                if problem:
                    missing.append(f"{label}: {problem}")
            if missing:
                self.add(f"transport.{tid}.credentials", "transports", subject, "fail", "; ".join(missing),
                         "set the named variable or create the named file (doctor never prints a value)")
            self._transport_target(tid, t, subject)

    def _transport_target(self, tid: str, t: Any, subject: str) -> None:
        cid = f"transport.{tid}"
        if t.kind in ("replay",):
            p = self.ws.root / (t.path or "")
            ok = p.is_file()
            self.add(cid, "transports", subject, "ok" if ok else "fail",
                     f"recordings {'found' if ok else 'missing'}: {t.path}",
                     None if ok else f"record a session to {t.path}, or fix transports.{tid}.path")
            return
        if t.kind == "files":
            p = (self.ws.root / t.root) if t.root else self.ws.root
            ok = p.is_dir()
            self.add(cid, "transports", subject, "ok" if ok else "fail", f"root {p}",
                     None if ok else f"create {p}, or fix transports.{tid}.root")
            return
        if t.command:
            cwd = (self.ws.root / t.cwd) if t.cwd else self.ws.root
            found = find_binary(t.command[0], root=self.ws.root, cwd=cwd, env=self.env)
            self.add(cid, "transports", subject, "ok" if found else "fail",
                     f"program {found}" if found else f"program {t.command[0]!r} not found",
                     None if found else f"install {t.command[0]} or fix transports.{tid}.command")
            return
        if t.url:
            self._endpoint(cid, subject, t.url, f"transports.{tid}.url")
            return
        self.add(cid, "transports", subject, "fail", "neither a command nor a url is configured",
                 f"set transports.{tid}.command or transports.{tid}.url")

    def _endpoint(self, cid: str, subject: str, url: str, where: str) -> None:
        u = urlparse(url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host:
            self.add(cid, "transports", subject, "fail", f"url {url!r} has no host", f"fix {where}")
            return
        if host in LOCAL_HOSTS or self.probe:
            err = _tcp(host, port)
            self.add(cid, "transports", subject, "ok" if err is None else "fail",
                     f"{host}:{port} " + ("reachable" if err is None else f"not reachable: {err}"),
                     None if err is None else f"start the server at {host}:{port}, or fix {where}")
            return
        self.add(cid, "transports", subject, "ok", f"remote endpoint {host}:{port} configured (not contacted; "
                 "pass --probe to connect)")

    # --- 4. auth checks ---------------------------------------------------------------
    def check_auth(self) -> None:
        for sid, s in sorted(self.ws.config.sources.items()):
            if s.auth is None or not s.auth.check:
                continue
            argv = list(s.auth.check)
            subject = f"sources.{sid}.auth.check"
            fix = s.auth.instructions or f"fix the credentials for source {sid}"
            if not find_binary(argv[0], root=self.ws.root, cwd=self.ws.root, env=self.env):
                self.add(f"auth.{sid}", "auth", subject, "fail", f"program {argv[0]!r} not found",
                         f"install {argv[0]} or fix {subject}")
                continue
            timeout = min(float(s.auth.timeout_s or 10.0), AUTH_TIMEOUT_CAP)
            r = run_unattended(argv, env=self.env, cwd=self.ws.root, timeout_s=timeout)
            if r["ended"] == "timeout":
                self.add(f"auth.{sid}", "auth", subject, "fail",
                         f"interactive or hung: no answer after {r['elapsed_s']}s with stdin closed and no TTY "
                         f"(timeout {timeout:g}s): {' '.join(argv)}",
                         "give the check a non-interactive form (it must never prompt), then: " + fix)
            elif r["ended"] == "not_started":
                self.add(f"auth.{sid}", "auth", subject, "fail", f"could not start: {r['message']}",
                         f"fix {subject}")
            elif r["code"] != 0:
                self.add(f"auth.{sid}", "auth", subject, "fail",
                         f"exited with status {r['code']}" + (f": {_mask(r['tail'])}" if r["tail"] else ""), fix)
            else:
                self.add(f"auth.{sid}", "auth", subject, "ok", f"passed unattended in {r['elapsed_s']}s")

    # --- 5. destinations ----------------------------------------------------------------
    def check_destinations(self) -> None:
        from docket.config.models import DESTINATION_ADAPTERS
        for did, d in sorted(self.ws.config.destinations.items()):
            subject = f"destinations.{did} ({d.adapter})"
            if d.adapter not in DESTINATION_ADAPTERS:
                self.add(f"dest.{did}", "destinations", subject, "fail", "unknown adapter", "docket plugin list")
                continue
            if d.adapter in ("markdown_tasks", "markdown_agenda", "journal", "calendar_file"):
                self._dest_path(did, d, subject)
            elif d.adapter == "calendar_exec":
                self._dest_exec(did, d, subject)
            elif d.adapter in ("calendar_mcp", "message_mcp"):
                self._dest_mcp(did, d, subject)
            self._dest_capabilities(did, d, subject)

    def _dest_path(self, did: str, d: Any, subject: str) -> None:
        default = {"calendar_file": f"{did}.ics", "markdown_agenda": did}.get(d.adapter, f"{did}.md")
        p = self.ws.root / (d.path or default)
        try:
            p.resolve().relative_to(self.ws.root.resolve())
        except ValueError:
            self.add(f"dest.{did}.location", "destinations", subject, "warn", f"{p} is outside the workspace")
        if p.exists():
            target = p
            ok = os.access(target, os.W_OK)
            self.add(f"dest.{did}.location", "destinations", subject, "ok" if ok else "fail",
                     f"{p} exists and is {'writable' if ok else 'not writable'}",
                     None if ok else f"make {p} writable")
            return
        parent = p.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        ok = os.access(parent, os.W_OK | os.X_OK)
        self.add(f"dest.{did}.location", "destinations", subject, "ok" if ok else "fail",
                 f"{p} does not exist yet; created on first write" if ok else f"{parent} is not writable",
                 None if ok else f"make {parent} writable or fix destinations.{did}.path")

    def _dest_exec(self, did: str, d: Any, subject: str) -> None:
        from docket.cli.errors import DocketError
        from docket.destinations.exec_client import ExecClient
        prog = list(d.program or [])
        if not prog:
            self.add(f"dest.{did}.program", "destinations", subject, "fail", "no program configured",
                     f"set destinations.{did}.program")
            return
        if not prog[0].startswith("/") and "/" in prog[0]:
            prog[0] = str(self.ws.root / prog[0])
        if not find_binary(prog[0], root=self.ws.root, cwd=self.ws.root, env=self.env):
            self.add(f"dest.{did}.program", "destinations", subject, "fail", f"program {prog[0]!r} not found",
                     f"install the bridge or fix destinations.{did}.program")
            return
        client = ExecClient(prog, cwd=self.ws.root, env=self.env, declared_env=d.env,
                            timeout_s=min(float(d.timeout_s or 20.0), AUTH_TIMEOUT_CAP), log_path=None,
                            mode="live", name=did)
        try:
            hello = client.start()
            caps = sorted(client.capabilities)
            self.add(f"dest.{did}.program", "destinations", subject, "ok",
                     f"hello: protocol {hello.get('protocol')}, verbs {', '.join(caps)}")
            if "status" not in client.capabilities:
                self.add(f"dest.{did}.permission", "destinations", subject, "warn",
                         "the program declares no non-prompting status verb; doctor cannot read its platform "
                         "permission", "add a status verb to the bridge (it must never prompt)")
                return
            res = client.call("status", {})
            access = str((res or {}).get("access", "unknown")) if isinstance(res, dict) else str(res)
            ok = access in ("granted", "ok", "authorized")
            self.add(f"dest.{did}.permission", "destinations", subject, "ok" if ok else "fail",
                     f"platform permission: {access}",
                     None if ok else (str((res or {}).get("fix") or "") if isinstance(res, dict) else "")
                     or "grant the bridge program calendar access in the system settings, then rerun docket doctor")
        except DocketError as e:
            hung = "timed out" in e.message or "timeout" in e.message
            self.add(f"dest.{did}.program", "destinations", subject, "fail",
                     ("interactive or hung: " if hung else "") + f"{e.code}: {e.message}",
                     e.hint or f"fix destinations.{did}.program")
        finally:
            client.close()

    def _dest_mcp(self, did: str, d: Any, subject: str) -> None:
        tools = sorted(d.tools)
        tr = (d.policy or {}).get("transport") if isinstance(d.policy, dict) else None
        self.add(f"dest.{did}.tools", "destinations", subject, "ok" if tools else "warn",
                 f"tools: {', '.join(tools)}" if tools else "no tools declared",
                 None if tools else f"declare destinations.{did}.tools for the verbs it needs")
        if tr and tr not in self.ws.config.transports:
            self.add(f"dest.{did}.transport", "destinations", subject, "fail", f"transport {tr!r} is not configured",
                     f"add transports.{tr}")

    def _dest_capabilities(self, did: str, d: Any, subject: str) -> None:
        from docket.config.models import DESTINATION_ADAPTERS
        declared = sorted(DESTINATION_ADAPTERS[d.adapter])
        self.add(f"dest.{did}.capabilities", "destinations", subject, "ok",
                 "accepts " + (", ".join(declared) if declared else "no actions (writes the journal)"))

    # --- 6. hooks -------------------------------------------------------------------------
    def check_hooks(self) -> None:
        for hid, h in sorted(self.ws.config.hooks.items()):
            cwd = (self.ws.root / h.cwd) if h.cwd else self.ws.root
            for label, argv in (("command", h.command), ("dry_run", h.dry_run)):
                if not argv:
                    continue
                found = find_binary(argv[0], root=self.ws.root, cwd=cwd, env=self.env)
                self.add(f"hook.{hid}.{label}", "hooks", f"hooks.{hid}.{label}", "ok" if found else "fail",
                         f"program {found}" if found else f"program {argv[0]!r} not found",
                         None if found else f"install {argv[0]} or fix hooks.{hid}.{label}")


def _mask(text: str) -> str:
    """Auth output can echo a token; keep only its first line, and never more than 200 characters."""
    return text.splitlines()[0][:200] if text else text


def summarize(checks: list[Check]) -> dict[str, int]:
    out = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for c in checks:
        out[c.status] = out.get(c.status, 0) + 1
    return out
