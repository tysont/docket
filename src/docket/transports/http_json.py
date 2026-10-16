"""The ``http_json`` transport: plain JSON APIs over ``httpx``.

Request payload: ``{"method": "GET", "url": "/path or absolute", "params": {...},
"headers": {...}, "body": <json>}``. The transport adds its configured headers
and credentials after the request digest is taken, so neither reaches a record
or a recording. A status of 400 or more is a failure carrying the status and
the raw response text; a body over ``max_response_bytes`` is a truncation signal.
"""
from __future__ import annotations

from typing import Any

from docket.transports.base import Failure, Request, Response, Secrets, raw_message

_ERROR_TEXT = 2000


class HttpJsonTransport:
    kind = "http_json"

    def __init__(self, tid: str, *, base_url: str | None, headers: dict[str, str], timeout_s: float,
                 max_response_bytes: int | None, trust_env: bool, secrets: Secrets) -> None:
        self.id = tid
        self.base_url = base_url or ""
        self.headers = dict(headers)
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes
        self.trust_env = trust_env
        self.secrets = secrets
        self.info: dict[str, Any] = {"name": "http_json", "protocol": 1}
        self._client: Any = None

    async def open(self) -> Failure | None:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=self.timeout_s,
                                             trust_env=self.trust_env, follow_redirects=True)
        return None

    async def request(self, req: Request) -> Response:
        import httpx
        await self.open()
        p = req.payload
        method = str(p.get("method") or "GET").upper()
        url = str(p.get("url") or "")
        kw: dict[str, Any] = {}
        if p.get("params"):
            kw["params"] = {k: v for k, v in p["params"].items() if v is not None}
        if p.get("headers"):
            kw["headers"] = p["headers"]
        if p.get("body") is not None:
            kw["json"] = p["body"]
        try:
            r = await self._client.request(method, url, **kw)
        except httpx.TimeoutException as e:
            return Response(error=Failure("timeout", self.secrets.mask(f"{method} {url}: {raw_message(e)}"),
                                          retryable=True))
        except httpx.HTTPError as e:
            return Response(error=Failure("unavailable", self.secrets.mask(f"{method} {url}: {raw_message(e)}"),
                                          retryable=True))
        body = r.content
        if r.status_code >= 400:
            text = self.secrets.mask(body.decode("utf-8", "replace")[:_ERROR_TEXT])
            kind = "too_large" if r.status_code == 413 else "status"
            return Response(body=b"", status=r.status_code,
                            error=Failure(kind, f"HTTP {r.status_code}: {text}" if text else f"HTTP {r.status_code}",
                                          status=r.status_code, retryable=r.status_code >= 500 or r.status_code == 429))
        truncated = self.max_response_bytes is not None and len(body) > self.max_response_bytes
        return Response(body=body, status=r.status_code, truncated=truncated,
                        meta={"content_type": r.headers.get("content-type")})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
