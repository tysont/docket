"""The error model (TD §15.3).

A ``DocketError`` carries a catalog code, its kind, a one-line message, a hint
naming the command that fixes it, whether a retry can succeed, and details.
The exit status is a function of the kind.
"""
from __future__ import annotations

from typing import Any

from docket.cli.catalog import ERRORS, KINDS


class DocketError(Exception):
    def __init__(
        self,
        code: str,
        kind: str | None = None,
        message: str = "",
        hint: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        *,
        next_command: list[str] | None = None,
    ) -> None:
        if code not in ERRORS:
            raise ValueError(f"error code {code!r} is not in the catalog")
        cat_kind, cat_retry, desc = ERRORS[code]
        if kind is not None and kind != cat_kind:
            raise ValueError(f"error code {code!r} has kind {cat_kind!r}, not {kind!r}")
        self.code = code
        self.kind = cat_kind
        self.message = message or desc
        self.hint = hint
        self.retryable = cat_retry if retryable is None else retryable
        self.details = details or {}
        self.next_command = next_command
        super().__init__(f"{code}: {self.message}")

    @property
    def exit_code(self) -> int:
        return KINDS[self.kind]

    def body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            "details": self.details,
        }


def _sub(kind: str) -> type[DocketError]:
    class _Kind(DocketError):
        def __init__(self, code: str, message: str = "", **kw: Any) -> None:
            super().__init__(code, kind, message, **kw)

    _Kind.__name__ = kind.title() + "Error"
    return _Kind


InputError = _sub("input")
ConfigError = _sub("config")
IntegrityError = _sub("integrity")
ExternalError = _sub("external")
LeaseError = _sub("lease")
StateError = _sub("state")
