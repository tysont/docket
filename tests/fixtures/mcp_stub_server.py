"""A stub MCP server for transport tests, written with the official SDK.

``python mcp_stub_server.py`` serves stdio; ``python mcp_stub_server.py --http PORT``
serves streamable HTTP on 127.0.0.1. Tools:

- ``list_items(token, page_size)``: pages of ROWS entries as JSON text; a page above 4 rows
  is refused as too large, like a gateway with a response cap;
- ``boom()``: fails with the server's own message;
- ``slow(seconds)``: sleeps;
- ``leak()``: returns the value of ``STUB_TOKEN`` (to prove masking);
- ``execute(code)``: a code-execution tool. The stub cannot run code; it accepts the
  form ``CALL <tool> <json args>`` and dispatches it, which is enough to prove that the
  codemode transport rendered its script and parameters.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

ROWS = [{"id": f"m-{i:03d}", "text": f"invented message {i}", "at": f"2026-10-15T0{i}:00:00Z"} for i in range(9)]
server = MCPServer("stub-mcp", log_level="ERROR")


def _page(token: str | None, page_size: int) -> str:
    if page_size > 4:
        raise ToolError(f"response too large: {page_size} rows requested, cap is 4")
    start = int(token or 0)
    chunk = ROWS[start:start + page_size]
    nxt = start + len(chunk)
    return json.dumps({"items": chunk, "next": str(nxt) if nxt < len(ROWS) else None})


@server.tool()
def list_items(token: str | None = None, page_size: int = 4) -> str:
    return _page(token, page_size)


@server.tool()
def boom() -> str:
    raise ToolError("shard 7 offline (code 503)")


@server.tool()
async def slow(seconds: float = 2.0) -> str:
    await asyncio.sleep(seconds)
    return "{}"


@server.tool()
def leak() -> str:
    raise ToolError("upstream rejected token " + os.environ.get("STUB_TOKEN", "unset"))


@server.tool()
def execute(code: str) -> str:
    head, _, rest = code.strip().partition(" ")
    tool, _, raw = rest.partition(" ")
    if head != "CALL" or tool != "list_items":
        raise ToolError(f"cannot run this code: {code[:80]}")
    a = json.loads(raw)
    return _page(a.get("token"), int(a.get("page_size") or 4))


if __name__ == "__main__":
    if "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http") + 1])
        server.run("streamable-http", host="127.0.0.1", port=port, json_response=True, stateless_http=True)
    else:
        server.run()
