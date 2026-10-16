"""State locations (TD §4.1).

The state root for a workspace is ``<base>/<workspace-slug>/`` where ``<base>``
is ``$DOCKET_STATE_ROOT`` when set, else ``$HOME/.local/state/docket``. Both
come from the environment the CLI was given, never from the process globals,
so a test's fake ``HOME`` is honored.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


def state_base(env: Mapping[str, str]) -> Path:
    if env.get("DOCKET_STATE_ROOT"):
        return Path(env["DOCKET_STATE_ROOT"]).expanduser()
    home = env.get("HOME")
    if not home:
        raise RuntimeError("HOME is not set and DOCKET_STATE_ROOT is not given")
    return Path(home) / ".local" / "state" / "docket"


def workspace_slug(root: Path) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "workspace"
    return f"{name}-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]}"


def state_root(root: Path, env: Mapping[str, str]) -> Path:
    return state_base(env) / workspace_slug(root)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
