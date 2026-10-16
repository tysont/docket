"""Build transport instances from ``transports:`` configuration.

Credentials are resolved here, at use, from the invoking environment or a
file, registered with the run's ``Secrets`` so every output path can mask
them, and handed to the transport only (TD §16). A missing credential is
reported as a problem naming the variable or file, never a value.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from docket.config.source_models import TransportConfig
from docket.transports.base import Failure, Secrets, child_env, resolve_credential


def _resolve_all(refs: Mapping[str, Any], env: Mapping[str, str], root: Path,
                 secrets: Secrets) -> tuple[dict[str, str], str | None]:
    out: dict[str, str] = {}
    for name, ref in refs.items():
        val, problem = resolve_credential(ref, env, root, secrets)
        if problem:
            return out, problem
        out[name] = val or ""
    return out, None


def build_transport(tid: str, cfg: TransportConfig, *, root: Path, env: Mapping[str, str], log_dir: Path | None,
                    secrets: Secrets) -> tuple[Any | None, Failure | None]:
    """Return (transport, None) or (None, failure). Nothing is started here."""
    headers = dict(cfg.headers)
    extra, problem = _resolve_all(cfg.headers_from, env, root, secrets)
    if problem:
        return None, Failure("unavailable", problem)
    headers.update(extra)
    if cfg.bearer is not None:
        tok, problem = resolve_credential(cfg.bearer, env, root, secrets)
        if problem:
            return None, Failure("unavailable", problem)
        headers["Authorization"] = f"Bearer {tok}"
    child_vars, problem = _resolve_all(cfg.env_from, env, root, secrets)
    if problem:
        return None, Failure("unavailable", problem)
    cenv = child_env(env, {**cfg.env, **child_vars})
    cwd = (root / cfg.cwd) if cfg.cwd else root
    common = {"timeout_s": cfg.timeout_s, "startup_timeout_s": cfg.startup_timeout_s,
              "max_response_bytes": cfg.max_response_bytes, "secrets": secrets}
    t: Any
    if cfg.kind == "files":
        from docket.transports.files import FilesTransport
        t = FilesTransport(tid, (root / cfg.root) if cfg.root else root)
    elif cfg.kind == "replay":
        from docket.transports.replay import ReplayTransport
        t = ReplayTransport(tid, root / (cfg.path or ""))
    elif cfg.kind == "exec_jsonl":
        from docket.transports.exec_jsonl import ExecJsonlTransport
        t = ExecJsonlTransport(tid, cfg.command or [], env=cenv, cwd=cwd, log_dir=log_dir, **common)
    elif cfg.kind == "http_json":
        from docket.transports.http_json import HttpJsonTransport
        t = HttpJsonTransport(tid, base_url=cfg.url, headers=headers, timeout_s=cfg.timeout_s,
                              max_response_bytes=cfg.max_response_bytes, trust_env=cfg.trust_env, secrets=secrets)
    elif cfg.kind in ("mcp_stdio", "mcp_http", "codemode"):
        from docket.transports.mcp_http import McpHttpTransport
        from docket.transports.mcp_stdio import McpStdioTransport
        if cfg.command:
            sess: Any = McpStdioTransport(tid, cfg.command, env=cenv, cwd=cwd, log_dir=log_dir, **common)
        else:
            sess = McpHttpTransport(tid, cfg.url or "", headers=headers, **common)
        if cfg.kind == "codemode":
            from docket.transports.codemode import CodemodeTransport
            t = CodemodeTransport(tid, sess, tool=cfg.tool or "", code_arg=cfg.code_arg)
        else:
            t = sess
    else:  # validation refuses unknown kinds; this is the safe reading for a direct caller
        return None, Failure("unavailable", f"unknown transport kind {cfg.kind!r}")
    if cfg.record is not None:
        from docket.transports.replay import Recorder
        t = Recorder(t, root / cfg.record.path, secrets, cfg.record.redact)
    return t, None
