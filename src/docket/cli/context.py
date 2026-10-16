"""Per-invocation context: environment, workspace, store and run resolution."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import IO, Any, Mapping

from docket.cli.errors import ConfigError
from docket.cli.registry import Command
from docket.store import lease
from docket.store.paths import state_root
from docket.store.runs import Store, resolve_run


class Ctx:
    def __init__(self, *, env: Mapping[str, str], cwd: Path, stdin: IO[str] | None,
                 command: Command | None, ns: argparse.Namespace | None) -> None:
        self.env = env
        self.cwd = cwd
        self.stdin = stdin
        self.command = command
        self.ns = ns or argparse.Namespace()
        self.warnings: list[dict[str, str]] = []
        self.run_id: str | None = None
        self._root: Path | None = None
        self._ws: Any = None
        self._store: Store | None = None
        self._rc: Any = None

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.ns, "dry_run", False))

    def warn(self, code: str, message: str) -> None:
        self.warnings.append({"code": code, "message": message})

    # --- workspace ---------------------------------------------------------
    def workspace_root(self) -> Path:
        if self._root is None:
            from docket.config.loader import discover_workspace
            root = discover_workspace(self.cwd, getattr(self.ns, "workspace", None), self.env)
            if root is None:
                raise ConfigError(
                    "workspace.not_found",
                    f"No docket.yaml found (looked at --workspace, DOCKET_WORKSPACE and up from {self.cwd}).",
                    hint="docket init <dir>, or pass --workspace <dir>",
                )
            self._root = root
        return self._root

    @property
    def workspace_path(self) -> str | None:
        return str(self._root) if self._root else None

    def workspace(self) -> Any:
        if self._ws is None:
            from docket.config import load_workspace
            self._ws = load_workspace(self.workspace_root())
        return self._ws

    def store(self) -> Store:
        if self._store is None:
            self._store = Store(state_root(self.workspace_root(), self.env))
        return self._store

    def state_root_or_none(self) -> Path | None:
        try:
            return self.store().root if self._root else None
        except Exception:
            return None

    # --- runs --------------------------------------------------------------
    def start_hint(self) -> str:
        try:
            wfs = list(self.workspace().config.workflows)
        except Exception:
            wfs = []
        return f"docket run start {wfs[0] if wfs else '<workflow>'}"

    def run_context(self, run_id: str | None = None) -> Any:
        if self._rc is None:
            from docket.engine.context import RunContext
            ws = self.workspace()
            run = resolve_run(self.store(), run_id or getattr(self.ns, "run", None), start_hint=self.start_hint())
            self.run_id = run.id
            self._rc = RunContext(ws, run, self.env)
        return self._rc

    def token(self, rc: Any, *, create: bool) -> str | None:
        return lease.session_token(rc.run.dir, self.env, create=create)

    def acquire(self, rc: Any, *, force: bool = False) -> dict[str, Any]:
        tok = self.token(rc, create=True)
        db = rc.run.store.db
        with db:
            rep = lease.acquire(db, rc.run.id, tok, steal_cmd=rc.argv("run", "steal", rc.run.id), force=force)
        if rep["adopted_from"]:
            self.warn("lease.adopted", f"Adopted the lease from dead session {rep['adopted_from']}.")
        if rep["stolen_from"]:
            self.warn("lease.stolen", f"Took the lease from session {rep['stolen_from']}.")
        return rep

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
