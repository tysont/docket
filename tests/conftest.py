"""Test isolation (Test Plan §2), enforced for every test by an autouse fixture.

- a fake HOME and a fake state root under the test's temporary directory;
- every environment variable whose name looks like a credential is removed;
- no network: sockets may connect to localhost only;
- a pinned clock helper; derived output never reads the wall clock;
- the CLI runs in process through ``docket.cli.main``.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from docket.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
PERSONAS = FIXTURES / "personas"
PINNED_NOW = "2026-10-15T18:00:00-05:00"
CREDENTIAL = re.compile(r"TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|AUTH", re.I)
_LOCAL = {"127.0.0.1", "::1", "localhost"}


class NetworkBlocked(RuntimeError):
    pass


def _guard(orig):
    def connect(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and (host in _LOCAL or host.startswith("/")):
            return orig(self, address, *a, **kw)
        raise NetworkBlocked(f"test tried to connect to {host!r}")
    return connect


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOCKET_STATE_ROOT", str(state))
    for name in list(os.environ):
        if CREDENTIAL.search(name):
            monkeypatch.delenv(name, raising=False)
    for name in ("DOCKET_WORKSPACE", "DOCKET_SESSION", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", _guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(socket.socket.connect_ex))
    real_cc = socket.create_connection

    def create_connection(address, *a, **kw):
        if address[0] not in _LOCAL:
            raise NetworkBlocked(f"test tried to connect to {address[0]!r}")
        return real_cc(address, *a, **kw)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    return {"home": home, "state": state}


@pytest.fixture
def pinned_now() -> str:
    return PINNED_NOW


class CLI:
    """Calls ``docket.cli.main`` in process and returns (exit code, parsed envelope)."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.last_stdout = ""
        self.last_stderr = ""

    def __call__(self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
                 stdin: str | None = None, json_out: bool = True) -> tuple[int, Any]:
        argv = list(args)
        if json_out and "--json" not in argv:
            argv.append("--json")
        out, err = io.StringIO(), io.StringIO()
        e = dict(os.environ)
        if env:
            e.update(env)
        code = main(argv, stdout=out, stderr=err, env=e, cwd=str(cwd or self.cwd),
                    stdin=io.StringIO(stdin) if stdin is not None else None)
        self.last_stdout, self.last_stderr = out.getvalue(), err.getvalue()
        if not json_out:
            return code, out.getvalue()
        return code, json.loads(out.getvalue())


@pytest.fixture
def cli(tmp_path: Path) -> CLI:
    return CLI(tmp_path)


def copy_persona(name: str, dest: Path) -> Path:
    target = dest / name
    shutil.copytree(PERSONAS / name, target)
    return target


@pytest.fixture
def persona(tmp_path: Path):
    def make(name: str, under: str = "ws") -> Path:
        base = tmp_path / under
        base.mkdir(exist_ok=True)
        return copy_persona(name, base)
    return make
