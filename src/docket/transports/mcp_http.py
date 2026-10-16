"""The ``mcp_http`` transport: an MCP server over streamable HTTP (TD §6.3).

A bearer token is read from a file or an environment variable at open and
added as a header; it never appears in configuration text, a request payload
or a record.
"""
from __future__ import annotations

from typing import Any

from docket.transports.mcp_stdio import McpTransport


class McpHttpTransport(McpTransport):
    kind = "mcp_http"

    def __init__(self, tid: str, url: str, *, headers: dict[str, str], **kw: Any) -> None:
        super().__init__(tid, **kw)
        self.url = url
        self.headers = dict(headers)
        self._http: Any = None

    def _connect(self) -> Any:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        self._http = create_mcp_http_client(headers=self.headers)
        return _Owned(self._http, Client(streamable_http_client(self.url, http_client=self._http), mode="legacy"))


class _Owned:
    """Enter an HTTP client and the MCP client together, closing both in reverse order."""

    def __init__(self, http: Any, client: Any) -> None:
        self.http = http
        self.client = client

    async def __aenter__(self) -> Any:
        await self.http.__aenter__()
        try:
            return await self.client.__aenter__()
        except BaseException:
            await self.http.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.client.__aexit__(*exc)
        finally:
            await self.http.__aexit__(None, None, None)
