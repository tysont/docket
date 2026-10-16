"""``calendar_exec``: a calendar behind a local bridge program speaking the exec protocol (TD §11.3, §17.2).

Verbs: ``hello``, ``list``, ``busy``, ``create``, ``move``, ``update``,
``delete``, ``rekey``, ``mirror``, ``status``. Docket builds operations,
ownership markers and redaction itself; the program only performs them. The
adapter's capabilities are the write verbs the program declares in ``hello``.

The twin is the program's own sandbox mode when ``hello`` declares
``sandbox: true``, and otherwise the generic in-memory calendar seeded from the
live listing. ``mirror`` and ``status`` are exposed on the backend for planning
and ``docket doctor``; apply never calls them.
"""
from __future__ import annotations

from typing import Any

from docket.destinations.calendar import CalendarAdapter, Event
from docket.destinations.exec_client import ExecClient
from docket.shapes.unexpected import unexpected

WRITE_VERBS = ("create", "move", "update", "delete", "rekey")
ALL_VERBS = ("hello", "list", "busy", "create", "move", "update", "delete", "rekey", "mirror", "status")


class ExecBackend:
    name = "exec"

    def __init__(self, client: ExecClient, factory: Any = None) -> None:
        self.client = client
        self.factory = factory  # mode -> ExecClient, for the sandbox twin

    def _events(self, res: Any) -> list[Event]:
        rows = res.get("events") if isinstance(res, dict) else res
        if not isinstance(rows, list):
            unexpected("dest.exec", "list result has no events array", sample=str(res)[:200],
                       reading="treat the calendar as empty", extend=f"{self.client.name}: return {{events: [...]}}")
            return []
        return [Event.from_dict(r, f"exec program {self.client.name}") for r in rows if isinstance(r, dict)]

    def list(self, calendar: str | None, window: tuple[str, str] | None) -> list[Event]:
        args: dict[str, Any] = {"calendar": calendar}
        if window:
            args.update({"from": window[0], "to": window[1]})
        return self._events(self.client.call("list", args))

    def busy(self, calendar: str | None, window: tuple[str, str]) -> list[list[str]]:
        res = self.client.call("busy", {"calendar": calendar, "from": window[0], "to": window[1]})
        return list((res or {}).get("busy") or []) if isinstance(res, dict) else []

    def create(self, calendar: str | None, key: str, start: str, end: str, title: str, notes: str) -> str:
        res = self.client.call("create", {"calendar": calendar, "key": key, "start": start, "end": end,
                                          "title": title, "notes": notes})
        return str((res or {}).get("external_id") or "") if isinstance(res, dict) else str(res)

    def move(self, calendar: str | None, ev: Event, key: str, start: str, end: str) -> None:
        self.client.call("move", {"calendar": calendar, "id": ev.id, "key": key, "start": start, "end": end})

    def update(self, calendar: str | None, ev: Event, key: str, title: str, notes: str) -> None:
        self.client.call("update", {"calendar": calendar, "id": ev.id, "key": key, "title": title, "notes": notes})

    def delete(self, calendar: str | None, ev: Event, key: str) -> None:
        self.client.call("delete", {"calendar": calendar, "id": ev.id, "key": key})

    def rekey(self, calendar: str | None, ev: Event, key: str, new_key: str, notes: str) -> None:
        self.client.call("rekey", {"calendar": calendar, "id": ev.id, "key": key, "new_key": new_key, "notes": notes})

    def mirror(self, args: dict[str, Any]) -> Any:
        return self.client.call("mirror", args)

    def status(self) -> Any:
        return self.client.call("status", {})

    def twin(self) -> "ExecBackend | None":
        self.client.start()
        if self.client.hello.get("sandbox") and self.factory is not None:
            return ExecBackend(self.factory("sandbox", self.client.mode), self.factory)
        return None

    def close(self) -> None:
        self.client.close()


def make_client(dest_id: str, cfg: Any, denv: Any, mode: str, base: str | None = None) -> ExecClient:
    prog = list(cfg.program or [])
    if prog and not prog[0].startswith("/") and "/" in prog[0]:
        prog[0] = str(denv.root / prog[0])
    log = (denv.log_dir / f"{dest_id}-{mode}.log") if denv.log_dir else None
    return ExecClient(prog, cwd=denv.root, env=denv.env, declared_env=cfg.env,
                      timeout_s=float(cfg.timeout_s or 20.0), log_path=log, mode=mode, base=base, name=dest_id)


class CalendarExec(CalendarAdapter):
    adapter = "calendar_exec"

    def __init__(self, *a: Any, mode: str = "live", **kw: Any) -> None:
        self.mode = mode
        super().__init__(*a, **kw)

    def client(self, mode: str, base: str | None = None) -> ExecClient:
        return make_client(self.id, self.cfg, self.denv, mode, base)

    def make_backend(self) -> Any:
        return ExecBackend(self.client(self.mode), self.client)

    def backend_verbs(self) -> set[str]:
        b = self.backend
        if isinstance(b, ExecBackend):
            return b.client.capabilities & set(WRITE_VERBS)
        return set(WRITE_VERBS)
