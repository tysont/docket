"""The ``codemode`` transport: an MCP server exposing one code-execution tool (TD §6.3).

Request payload: ``{"script": "<template>", "args": {...}}``. The preset ships the
script; it pages, projects to declared fields and returns compact JSON under the
server's response cap. Placeholders ``{{name}}`` are replaced by the JSON
encoding of ``args[name]`` (``null`` when absent), so a parameter can never
break out of its literal. The rendered script is sent as the tool's
``code_arg`` argument over the underlying MCP session (stdio or streamable HTTP).
"""
from __future__ import annotations

import json
import re
from typing import Any

from docket.transports.base import Failure, Request, Response

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


def render_script(template: str, args: dict[str, Any]) -> str:
    def sub(m: re.Match[str]) -> str:
        cur: Any = args
        for part in m.group(1).split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return json.dumps(cur, sort_keys=True, ensure_ascii=False)
    return _PLACEHOLDER.sub(sub, template)


class CodemodeTransport:
    kind = "codemode"

    def __init__(self, tid: str, session: Any, *, tool: str, code_arg: str = "code") -> None:
        self.id = tid
        self.session = session  # an McpStdioTransport or McpHttpTransport
        self.tool = tool
        self.code_arg = code_arg

    @property
    def info(self) -> dict[str, Any]:
        return self.session.info

    async def open(self) -> Failure | None:
        return await self.session.open()

    async def request(self, req: Request) -> Response:
        script = req.payload.get("script")
        if not isinstance(script, str):
            return Response(error=Failure("protocol", "codemode request has no script"))
        code = render_script(script, dict(req.payload.get("args") or {}))
        return await self.session.call(self.tool, {self.code_arg: code})

    async def close(self) -> None:
        await self.session.close()
