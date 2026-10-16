"""Write the persona fixtures' recorded transports (TP §7.1).

Each persona keeps the upstream truth in ``data/``. This script plays a small
fake of every upstream system (a chat gateway's code tool, a mail server, two
JSON APIs, three exec bridges), drives the real collection pipeline against
them through ``Recorder``, and writes ``recordings/<transport>.jsonl``. It runs
two passes: a first run from no cursor, and a follow-up from the first run's
proposals, which is what a refresh or the next run requests.

Run from the repository root after changing a preset's request plan:

    uv run python tests/fixtures/make_recordings.py

``tests/unit/sources/test_recordings_fresh.py`` fails when the files are stale.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from docket.config import load_workspace  # noqa: E402
from docket.engine.clock import pin, run_date  # noqa: E402
from docket.sources import collect as collect_mod  # noqa: E402
from docket.transports import registry  # noqa: E402
from docket.transports.base import Failure, Request, Response, Secrets  # noqa: E402
from docket.transports.replay import Recorder  # noqa: E402

PERSONAS = Path(__file__).resolve().parent / "personas"
PINNED_NOW = "2026-10-15T18:00:00-05:00"


def _t(v: Any) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _after(rows: list[dict[str, Any]], key: str, after: Any) -> list[dict[str, Any]]:
    a = _t(after)
    out = [r for r in rows if r.get(key) and (a is None or _t(r[key]) > a)]
    return sorted(out, key=lambda r: (_t(r[key]), json.dumps(r, sort_keys=True)))


def _page(rows: list[Any], token: Any, size: int) -> tuple[list[Any], str | None]:
    start = int(token or 0)
    chunk = rows[start:start + size]
    nxt = start + size
    return chunk, (str(nxt) if nxt < len(rows) else None)


def _json(v: Any) -> Response:
    return Response(body=json.dumps(v, sort_keys=True).encode())


class Fake:
    kind = "fake"

    def __init__(self, tid: str, handler: Callable[[dict[str, Any]], Response]) -> None:
        self.id, self.handler, self.info = tid, handler, {"name": "fake", "protocol": 1}

    async def open(self) -> Failure | None:
        return None

    async def request(self, req: Request) -> Response:
        return self.handler(req.payload)

    async def close(self) -> None:
        return None


# --- reference-like ---------------------------------------------------------------

SPACES = {
    "spaces/platform-room": ("Platform Room", "ROOM", 6),
    "spaces/dm-kim": ("Direct message", "DM", 2),
    "spaces/ingest-war-room": ("Ingest War Room", "ROOM", 12),
    "spaces/release-train": ("Release Train", "ROOM", 30),
    "spaces/hallway": (None, "THREADED_ROOM", 40),  # nameless, of an unknown type, with no messages
}


def chat_gateway(ws: Path) -> Callable[[dict[str, Any]], Response]:
    msgs = _rows(ws / "data" / "chat.jsonl")
    msgs.append({"name": "spaces/platform-room/messages/m099", "space": "spaces/platform-room",
                 "text": "A message whose timestamp the gateway lost.", "sender": {"name": "users/2002"}})

    def handle(p: dict[str, Any]) -> Response:
        a = p.get("args") or {}
        if "space" not in a:
            spaces = [{"name": k, "displayName": v[0], "spaceType": v[1], "memberCount": v[2]}
                      for k, v in sorted(SPACES.items())]
            return Response(body=json.dumps({"spaces": [{k: x for k, x in s.items() if x is not None}
                                                        for s in spaces], "nextPageToken": None}).encode())
        mine = [m for m in msgs if m["space"] == a["space"]]
        dated = _after([m for m in mine if "createTime" in m], "createTime", a.get("after"))
        rows = dated + [m for m in mine if "createTime" not in m]  # a server that lost timestamps still returns them
        chunk, nxt = _page(rows, a.get("token"), int(a.get("page_size") or 100))
        out = [{"name": m["name"], "createTime": m.get("createTime"), "text": m.get("text", ""),
                "sender": {"name": (m.get("sender") or {}).get("name"),
                           "displayName": (m.get("sender") or {}).get("displayName")},
                "thread": {"name": (m.get("thread") or {}).get("name")}} for m in chunk]
        for o in out:
            if o["createTime"] is None:
                del o["createTime"]
            if o["sender"]["displayName"] is None:
                del o["sender"]["displayName"]
        return _json({"messages": out, "nextPageToken": nxt, "truncated": False})
    return handle


def mail_server(ws: Path) -> Callable[[dict[str, Any]], Response]:
    rows = [{"id": r["messageId"], "date": r["date"], "from": r["from"], "subject": r["subject"],
             "snippet": r["snippet"]} for r in _rows(ws / "data" / "mail.jsonl")]

    def handle(p: dict[str, Any]) -> Response:
        a = p.get("args") or {}
        chunk, nxt = _page(_after(rows, "date", a.get("after")), a.get("page_token"), int(a.get("page_size") or 50))
        return _json({"messages": chunk, "nextPageToken": nxt})
    return handle


def failing(status: int, message: str) -> Callable[[dict[str, Any]], Response]:
    return lambda p: Response(status=status, error=Failure("status", message, status, retryable=True))


def json_api(ws: Path, name: str) -> Callable[[dict[str, Any]], Response]:
    rows = _rows(ws / "data" / name)

    def handle(p: dict[str, Any]) -> Response:
        q = p.get("params") or {}
        chunk, nxt = _page(_after(rows, "updated_at", q.get("updated_after")), q.get("cursor"),
                           int(q.get("limit") or 100))
        return _json({"items": chunk, "next": nxt})
    return handle


def exec_bridge(ws: Path, name: str) -> Callable[[dict[str, Any]], Response]:
    rows = sorted(_rows(ws / "data" / name), key=lambda r: r["at"])

    def handle(p: dict[str, Any]) -> Response:
        a = p.get("args") or {}
        start = int(a.get("position") or 0)
        size = int(a.get("page_size") or 100)
        chunk = rows[start:start + size]
        nxt = start + len(chunk)
        return _json({"v": 1, "id": 0, "ok": True,
                      "result": {"items": chunk, "next": str(nxt), "end": nxt >= len(rows)}})
    return handle


FAKES: dict[str, dict[str, Callable[[Path], Callable[[dict[str, Any]], Response]]]] = {
    "reference-like": {
        "chat-gateway": chat_gateway, "mail-server": mail_server,
        "status-api": lambda ws: failing(503, "HTTP 503: upstream shard 7 offline; retry after maintenance"),
        "archive-api": lambda ws: json_api(ws, "archive.jsonl"),
        "notes-gateway": lambda ws: failing(500, "unused"), "wiki-feed": lambda ws: failing(500, "unused"),
    },
    "support-lead": {
        "pager-bridge": lambda ws: exec_bridge(ws, "pager.jsonl"),
        "metrics-api": lambda ws: failing(500, 'HTTP 500: {"error":"stats backend timeout after 30s"}'),
        "history-bridge": lambda ws: exec_bridge(ws, "history.jsonl"),
        "kb-bridge": lambda ws: failing(500, "unused"),
    },
}


def record(persona: str, out_root: Path | None = None) -> dict[str, Path]:
    """Write recordings for ``persona`` under ``out_root`` (default: the persona directory)."""
    ws_dir = PERSONAS / persona
    out = (out_root or ws_dir) / "recordings"
    out.mkdir(parents=True, exist_ok=True)
    ws = load_workspace(ws_dir)
    paths = {}
    for name in FAKES[persona]:
        p = out / f"{name}.jsonl"
        p.write_text("")
        paths[name] = p
    secrets = Secrets()
    fakes = {name: Recorder(Fake(name, make(ws_dir)), paths[name], secrets) for name, make in FAKES[persona].items()}
    real = registry.build_transport

    def build(tid: str, cfg: Any, **kw: Any) -> Any:
        return (fakes[tid], None) if tid in fakes else real(tid, cfg, **kw)

    registry.build_transport = build
    try:
        clock = pin(ws.config.profile.timezone, now=PINNED_NOW)
        sids = sorted(s for s in ws.config.sources if ws.config.sources[s].transport)
        starts = collect_mod.Starts()
        first = asyncio.run(collect_mod.read_sources(ws, sids, env={}, instant=clock.instant, run_date=run_date(clock),
                                                     starts=starts, log_dir=None, secrets=secrets))
        durable = {}
        for sid, res in first.items():
            for u in res.units:
                pos, cov, _ = u.proposal
                if pos is not None:
                    durable[(sid, u.unit.id)] = (pos, cov)
        asyncio.run(collect_mod.read_sources(ws, sids, env={}, instant=clock.instant, run_date=run_date(clock),
                                             starts=collect_mod.Starts(durable=durable), log_dir=None,
                                             secrets=secrets))
    finally:
        registry.build_transport = real
    for p in paths.values():
        lines = sorted(set(p.read_text().splitlines()))
        p.write_text("".join(ln + "\n" for ln in lines if ln.strip()))
    return paths


if __name__ == "__main__":
    for persona in FAKES:
        for name, path in record(persona).items():
            print(f"{persona}: {path.relative_to(ROOT)} ({len(path.read_text().splitlines())} pairs)")
