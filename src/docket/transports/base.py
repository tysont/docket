"""The transport port (TD §6.3).

A transport sends one request and returns one response body plus metadata. It
knows nothing about records, cursors or items. A failure is returned, never
raised: ``Response.error`` carries the transport's own message and status
verbatim, so a source outcome can show exactly what the system said (TD §6.6).

Every subprocess a transport starts runs without a controlling terminal, with
``DOCKET_NONINTERACTIVE=1`` and a scrubbed environment (TD §14.5).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from docket.config.digest import canonical_json, digest

FailureKind = Literal["timeout", "too_large", "status", "protocol", "unavailable", "no_recording", "error"]

# Failure kind -> catalog code (docket.cli.catalog, "M2 collection").
FAILURE_CODES: dict[str, str] = {
    "timeout": "transport.timeout",
    "too_large": "transport.too_large",
    "status": "transport.status",
    "protocol": "transport.protocol",
    "unavailable": "transport.unavailable",
    "no_recording": "transport.no_recording",
    "error": "transport.error",
}

# Variables a child process may inherit from the invoking environment. Everything
# else must be declared on the transport (``env`` or ``env_from``).
PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SYSTEMROOT")


@dataclass
class Failure:
    kind: FailureKind
    message: str  # the raw message, as the system gave it (masked for credentials)
    status: int | None = None
    retryable: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> str:
        return FAILURE_CODES[self.kind]

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "kind": self.kind, "message": self.message,
                             "status": self.status, "retryable": self.retryable}
        if self.data:
            d["data"] = self.data
        return d


@dataclass
class Request:
    """One request. ``payload`` is transport-specific and never holds a credential:
    credentials are added inside the transport, after the digest is taken."""
    payload: dict[str, Any]

    @property
    def digest(self) -> str:
        return digest(self.payload)

    def canonical(self) -> bytes:
        return canonical_json(self.payload)


@dataclass
class Response:
    body: bytes = b""
    status: int | None = None
    truncated: bool = False  # over the transport's size cap: a truncation signal (TD §6.5)
    error: Failure | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


class Transport(Protocol):
    id: str
    kind: str
    info: dict[str, Any]

    async def open(self) -> Failure | None: ...
    async def request(self, req: Request) -> Response: ...
    async def close(self) -> None: ...


# --- credentials and masking (TD §16) ----------------------------------------


class Secrets:
    """Values read from credential sources. ``mask`` removes them from any text."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> None:
        if value and len(value) >= 4:
            self._values.add(value)

    def mask(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, "[redacted]")
        return text

    def mask_bytes(self, data: bytes) -> bytes:
        if not self._values:
            return data
        return self.mask(data.decode("utf-8", "replace")).encode("utf-8")


def resolve_credential(ref: Any, env: Mapping[str, str], root: Path, secrets: Secrets) -> tuple[str | None, str | None]:
    """Read a credential reference ``{env: NAME}`` or ``{file: PATH}``. Returns (value, problem).

    The problem text names the variable or path, never a value.
    """
    name = getattr(ref, "env", None)
    path = getattr(ref, "file", None)
    if name:
        val = env.get(name)
        if not val:
            return None, f"credential environment variable {name} is not set"
        secrets.register(val)
        return val, None
    if path:
        p = Path(path)
        if p.parts and p.parts[0] == "~":
            home = env.get("HOME")
            if not home:
                return None, f"credential file {path} needs HOME"
            p = Path(home, *p.parts[1:])
        elif not p.is_absolute():
            p = root / p
        try:
            val = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            return None, f"credential file {path} cannot be read: {e.strerror}"
        if not val:
            return None, f"credential file {path} is empty"
        secrets.register(val)
        return val, None
    return None, "credential reference names neither env nor file"


def child_env(base: Mapping[str, str], declared: Mapping[str, str]) -> dict[str, str]:
    """A scrubbed environment: a few process basics, the declared variables, and the
    non-interactive marker."""
    out = {k: base[k] for k in PASS_ENV if k in base}
    out.update(declared)
    out["DOCKET_NONINTERACTIVE"] = "1"
    return out


def raw_message(exc: BaseException) -> str:
    """The original message of an exception, flattening exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        parts = [raw_message(e) for e in exc.exceptions]
        return "; ".join(p for p in parts if p) or type(exc).__name__
    msg = str(exc).strip()
    name = type(exc).__name__
    if not msg:
        return name
    return msg if msg.startswith(name) else f"{name}: {msg}"


# Request keys whose values are credentials wherever they appear. Paging keys such as
# ``pageToken`` are deliberately absent: they are positions, not secrets.
CREDENTIAL_KEYS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api_key", "apikey", "api-key",
    "password", "passwd", "secret", "client_secret", "access_token", "refresh_token", "id_token", "bearer",
})


def is_credential_key(key: str) -> bool:
    return key.lower() in CREDENTIAL_KEYS


def redact_mapping(value: Any) -> Any:
    """Replace every value under a credential key with ``[redacted]``."""
    if isinstance(value, dict):
        return {k: ("[redacted]" if is_credential_key(str(k)) and v not in (None, "") else redact_mapping(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_mapping(v) for v in value]
    return value


def open_log(log_dir: Path | None, name: str) -> Any:
    """The file a child's stderr goes to: ``<log_dir>/<name>.stderr.log``, or the null
    device when nothing durable may be written (``docket source test``)."""
    if log_dir is None:
        return open(os.devnull, "w", encoding="utf-8")
    log_dir.mkdir(parents=True, exist_ok=True)
    return (log_dir / f"{name}.stderr.log").open("a", encoding="utf-8")



def kill_group(proc: Any) -> None:
    """Kill a child started with ``start_new_session`` and everything it spawned, so no
    grandchild keeps its pipes open."""
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
