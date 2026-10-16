"""MCP destinations: ``calendar_mcp`` and ``message_mcp`` (TD §11.3).

Both are written against a transport-agnostic ``call_tool(name, args)``
callable, so they test against a fake server and take the real MCP transport
when it exists. A destination's ``tools`` map names the server tool for each
verb; the preset declares them. ``calendar_mcp`` reads with ``list`` always and
writes only the verbs whose tool is declared. ``message_mcp`` creates drafts
only: ``policy.send`` is rejected by validation ("not in v1").

Tool results are normalized: ``list`` returns ``{"events": [...]}`` or
``{"drafts": [...]}``; ``create``/``draft`` return ``{"id": ...}``.
"""
from __future__ import annotations

import copy
from typing import Any

from docket.cli.errors import ExternalError, IntegrityError
from docket.destinations.base import (
    Action, BaseDestination, CallTool, DestEnv, DestState, Operation, Receipt, VerifyResult, make_op,
)
from docket.destinations.calendar import CalendarAdapter, Event, Marker, ownership_key
from docket.text import templates
from docket.text.redact import Redactor

CAL_VERBS = ("create", "move", "update", "delete", "rekey")


def unavailable(dest_id: str) -> CallTool:
    def call(name: str, args: dict[str, Any]) -> Any:
        raise ExternalError("dest.unavailable", f"{dest_id}: no MCP transport is configured for this destination",
                            hint="docket doctor", details={"destination": dest_id, "tool": name})
    return call


def tool_caller(dest_id: str, denv: DestEnv) -> CallTool:
    if denv.call_tool is not None:
        c = denv.call_tool(dest_id)
        if c is not None:
            return c
    return unavailable(dest_id)


class McpCalendarBackend:
    name = "mcp"

    def __init__(self, call: CallTool, tools: dict[str, str], dest_id: str) -> None:
        self.call, self.tools, self.dest_id = call, tools, dest_id

    def _tool(self, verb: str) -> str:
        t = self.tools.get(verb)
        if not t:
            raise IntegrityError("dest.unsupported", f"{self.dest_id}: no tool is declared for {verb}",
                                 hint=f"docket config explain destinations.{self.dest_id}.tools",
                                 details={"destination": self.dest_id, "verb": verb})
        return t

    def list(self, calendar: str | None, window: tuple[str, str] | None) -> list[Event]:
        args: dict[str, Any] = {"calendar": calendar}
        if window:
            args.update({"from": window[0], "to": window[1]})
        res = self.call(self._tool("list"), args) or {}
        rows = res.get("events") if isinstance(res, dict) else res
        return [Event.from_dict(r, f"destinations.{self.dest_id}.tools.list") for r in rows or [] if isinstance(r, dict)]

    def create(self, calendar: str | None, key: str, start: str, end: str, title: str, notes: str) -> str:
        res = self.call(self._tool("create"), {"calendar": calendar, "start": start, "end": end, "title": title,
                                               "notes": notes})
        return str((res or {}).get("id", "")) if isinstance(res, dict) else str(res)

    def move(self, calendar: str | None, ev: Event, key: str, start: str, end: str) -> None:
        self.call(self._tool("move"), {"calendar": calendar, "id": ev.id, "start": start, "end": end})

    def update(self, calendar: str | None, ev: Event, key: str, title: str, notes: str) -> None:
        self.call(self._tool("update"), {"calendar": calendar, "id": ev.id, "title": title, "notes": notes})

    def rekey(self, calendar: str | None, ev: Event, key: str, new_key: str, notes: str) -> None:
        self.call(self._tool("update"), {"calendar": calendar, "id": ev.id, "title": ev.title, "notes": notes})

    def delete(self, calendar: str | None, ev: Event, key: str) -> None:
        self.call(self._tool("delete"), {"calendar": calendar, "id": ev.id})

    def twin(self) -> None:
        return None

    def close(self) -> None:
        return None


class CalendarMcp(CalendarAdapter):
    adapter = "calendar_mcp"

    def make_backend(self) -> Any:
        return McpCalendarBackend(tool_caller(self.id, self.denv), dict(self.cfg.tools), self.id)

    def backend_verbs(self) -> set[str]:
        declared = set(self.cfg.tools)
        verbs = {v for v in CAL_VERBS if v in declared}
        if "update" in declared:
            verbs.add("rekey")
        return verbs


# --- message drafts -----------------------------------------------------------------


class MemoryDrafts:
    """The draft store twin: holds drafts in memory, seeded from the live listing."""

    def __init__(self, drafts: list[dict[str, Any]] | None = None) -> None:
        self.drafts = {d["id"]: copy.deepcopy(d) for d in drafts or []}
        self.n = 0

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        if name == "list":
            return {"drafts": [copy.deepcopy(d) for _, d in sorted(self.drafts.items())]}
        if name == "draft":
            self.n += 1
            did = f"draft-{self.n}"
            self.drafts[did] = {"id": did, **copy.deepcopy(args)}
            return {"id": did}
        raise IntegrityError("dest.unsupported", f"draft twin has no tool {name}", hint="docket plugin check message_mcp")


class MessageMcp(BaseDestination):
    """Creates message drafts. Never sends: v1 has no send verb (FR-ACT-05, IP §5)."""
    adapter = "message_mcp"
    capabilities = frozenset({"message.draft"})
    verbs = frozenset({"draft"})

    def __init__(self, dest_id: str, cfg: Any, denv: DestEnv, *, call: CallTool | None = None,
                 twin: bool = False) -> None:
        super().__init__(dest_id, cfg, denv, twin=twin)
        self.call = call or tool_caller(dest_id, denv)
        self.tools = dict(cfg.tools) if not twin else {"list": "list", "draft": "draft"}
        self.marker = Marker(cfg.marker, dest_id)
        self._redactor: Redactor | None = None

    def _tool(self, verb: str) -> str:
        t = self.tools.get(verb)
        if not t:
            raise IntegrityError("dest.unsupported", f"{self.id}: no tool is declared for {verb}",
                                 hint=f"docket config explain destinations.{self.id}.tools",
                                 details={"destination": self.id, "verb": verb})
        return t

    def drafts(self) -> list[dict[str, Any]]:
        res = self.call(self._tool("list"), {}) or {}
        rows = res.get("drafts") if isinstance(res, dict) else res
        return [r for r in rows or [] if isinstance(r, dict)]

    def owned(self) -> dict[str, dict[str, Any]]:
        out = {}
        for d in self.drafts():
            k = self.marker.find(str(d.get("body", "")))
            if k is not None and k not in out:
                out[k] = d
        return out

    def snapshot(self) -> DestState:
        rows = self.drafts()
        owned = {k: {"id": d.get("id"), "to": d.get("to"), "subject": d.get("subject"),
                     "body": self.marker.strip(str(d.get("body", "")))} for k, d in sorted(self.owned().items())}
        return DestState(destination=self.id, adapter=self.adapter,
                         data={"owned": owned, "unowned_ids": sorted(str(d.get("id")) for d in rows
                                                                     if self.marker.find(str(d.get("body", ""))) is None)})

    def sandbox(self) -> "MessageMcp":
        return MessageMcp(self.id, self.cfg, self.denv, call=MemoryDrafts([
            {**d, "id": str(d.get("id"))} for d in self.drafts()]), twin=True)

    @property
    def redactor(self) -> Redactor:
        if self._redactor is None:
            self._redactor = Redactor.from_config(self.denv.profile, self.cfg.redact)
        return self._redactor

    def preview(self, action: Action, state: DestState, **_: Any) -> list[Operation]:
        self.require(action)
        ctx = action.fields
        if not action.key:
            raise IntegrityError("dest.not_accepted", f"{self.id}: a draft needs a key seed",
                                 hint="docket plan build", details={"destination": self.id})
        key = ownership_key(self.id, action.key)
        subject = templates.render(self.cfg.title_template or "{title}", ctx)
        body = templates.render(self.cfg.notes_template or "{note}", ctx)
        if self.visibility == "shared":
            subject, body = self.redactor.render_title(self.cfg.title_template or "{title}", ctx), \
                self.redactor.render_text(self.cfg.notes_template or "{note}", ctx)
            self.redactor.check(subject, where=f"destinations.{self.id}.subject")
            self.redactor.check(body, where=f"destinations.{self.id}.body")
        to = ctx.get("to") or []
        return [make_op(self.id, "draft", ident=key, key=key, to=list(to) if isinstance(to, list) else [str(to)],
                        subject=subject, body=body, question_id=action.question_id, item_id=action.item_id,
                        depends_on=action.depends_on or None)]

    def apply(self, op: Operation) -> Receipt:
        self.require_verb(op)
        cur = self.owned().get(op.key or "")
        if cur is not None:
            if cur.get("subject") == op.subject and self.marker.strip(str(cur.get("body", ""))) == (op.body or "").rstrip():
                return self.receipt(op, "already_present", str(cur.get("id")))
            raise IntegrityError("dest.state_changed", f"{self.id}: an owned draft with key {op.key} exists with "
                                 "different content", hint="docket plan build",
                                 details={"destination": self.id, "key": op.key})
        res = self.call(self._tool("draft"), {"to": op.to, "subject": op.subject,
                                              "body": self.marker.attach(op.body or "", op.key)})
        return self.receipt(op, "applied", str((res or {}).get("id", "")) if isinstance(res, dict) else str(res))

    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult:
        cur = self.owned().get(op.key or "")
        if cur is None:
            return VerifyResult(ok=False, status="missing")
        ok = cur.get("subject") == op.subject
        return VerifyResult(ok=ok, status="verified" if ok else "mismatch")
