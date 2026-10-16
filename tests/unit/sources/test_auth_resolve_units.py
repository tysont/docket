"""Unattended auth checks (TD §6.8, §14.5), resolve (TD §6.10) and unit classification (TD §6.9)."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import textwrap

import pytest

from docket.config import load_workspace
from docket.config.source_models import Source, UnitRule
from docket.sources.base import Unit
from docket.sources.declarative import DeclarativeSource, Limits
from docket.sources.resolve import resolve_ref
from docket.sources.units import classify, upsert_units
from docket.store.db import open_db
from docket.transports.base import Response, Secrets

pytestmark = pytest.mark.smoke


def auth_source(check, timeout=2.0):
    cfg = Source.model_validate({"transport": "t", "request": {"read": {"op": "read"}},
                                 "auth": {"check": check, "timeout_s": timeout,
                                          "instructions": "Run stub-login, then collect again."}})
    return DeclarativeSource("s", cfg, None, secrets=Secrets(), limits=Limits(asyncio.Semaphore(1),
                                                                              asyncio.Semaphore(1)),
                             env=dict(os.environ))


@pytest.mark.parametrize("flag,ok,needle", [
    ("--ok", True, None),
    ("--env", True, None),            # DOCKET_NONINTERACTIVE=1 reaches the check
    ("--stdin", True, None),          # stdin is closed: a prompt reads end-of-file
    ("--fail", False, "exited with status 3: stub-sso: session expired (code 401)"),
])
def test_auth_checks_run_unattended(flag, ok, needle):
    st = asyncio.run(auth_source(["docket-stub-auth", flag]).check_auth())
    assert st.ok is ok
    if needle:
        assert needle in st.reason and st.instructions == "Run stub-login, then collect again."


def test_an_auth_check_that_waits_is_treated_as_interactive():
    st = asyncio.run(auth_source(["docket-stub-auth", "--hang"], timeout=0.3).check_auth())
    assert not st.ok and "timed out after 0.3s (treated as interactive)" in st.reason
    st = asyncio.run(auth_source(["docket-no-such-check"]).check_auth())
    assert not st.ok and "could not start" in st.reason


def test_resolve_claims_by_pattern_and_reports_unclaimed_refs(tmp_path):
    ws_dir = tmp_path / "ws"
    (ws_dir / "rec").mkdir(parents=True)
    (ws_dir / "docket.yaml").write_text(textwrap.dedent("""\
        version: 1
        workflows: { daily: { stages: [collect, assess, merge, decide, plan, approve, apply, close] } }
        profile: { timezone: UTC, me: { name: Dana Reyes } }
        destinations: { backlog: { adapter: markdown_tasks, path: tasks.md } }
        transports: { bridge: { kind: replay, path: rec/bridge.jsonl } }
        sources:
          tracker:
            extends: exec/jsonl-source
            transport: bridge
            resolve:
              patterns: ['\\b(?P<key>TRK-\\d+)\\b']
        """))
    req = {"op": "resolve", "args": {"ref": "see TRK-42 for details"}}
    body = {"v": 1, "id": 1, "ok": True, "result": {"state": "closed", "last_activity": "2026-10-14T10:00:00Z",
                                                    "last_actor": "users/9", "title": "Rotate the signing keys"}}
    (ws_dir / "rec" / "bridge.jsonl").write_text(json.dumps({"request": req, "response": {"body_json": body}}) + "\n")
    ws = load_workspace(ws_dir)
    got = resolve_ref(ws, "see TRK-42 for details", env={})
    assert (got.source, got.status, got.state, got.last_actor, got.title) == (
        "tracker", "resolved", "closed", "users/9", "Rotate the signing keys")
    assert resolve_ref(ws, "https://elsewhere.example/item/1", env={}).status == "unresolvable"
    miss = resolve_ref(ws, "TRK-43", env={})
    assert miss.status == "failed" and miss.cause["code"] == "transport.no_recording"


def test_unit_rules_apply_in_order_by_name_type_and_members(tmp_path):
    rules = [UnitRule.model_validate(r) for r in (
        {"id": "dm", "match": {"type": ["DM"]}, "set": {"kind": "direct", "tier": 1}},
        {"id": "ops", "match": {"name_regex": "(?i)ops"}, "set": {"class": "operations"}},
        {"id": "big", "match": {"members_min": 20}, "set": {"kind": "broadcast", "class": "broadcast", "tier": 3}},
        {"match": {"source": "other"}, "set": {"class": "never"}},
    )]
    assert classify(rules, "chat", Unit("a", "Ops Room", "ROOM", 25)) == (
        {"class": "operations", "kind": "broadcast", "tier": 3}, ["ops", "big"])
    assert classify(rules, "chat", Unit("b", "Pat", "DM", 2)) == ({"kind": "direct", "tier": 1}, ["dm"])
    assert classify(rules, "chat", Unit("c", "Quiet", "ROOM", None)) == ({}, [])

    class WS:
        class config:
            signals = {"units": {"rules": [{"id": "dm", "match": {"type": ["DM"]}, "set": {"kind": "direct"}}]}}
    db = open_db(tmp_path / "d.db")
    db.row_factory = sqlite3.Row
    with db:
        upsert_units(db, WS, "chat", [Unit("b", "Pat", "DM", 2), Unit("c", "Quiet", "ROOM")], "2026-10-15T00:00:00Z")
        db.execute("UPDATE units SET kind='team', class='team', classified_by='agent' WHERE unit_id='c'")
        upsert_units(db, WS, "chat", [Unit("b", "Pat", "DM", 2), Unit("c", "Quiet", "ROOM")], "2026-10-16T00:00:00Z")
    rows = {r["unit_id"]: dict(r) for r in db.execute("SELECT * FROM units")}
    assert rows["b"]["kind"] == "direct" and rows["b"]["classified_by"] == "rule" and rows["b"]["members"] == 2
    assert rows["b"]["first_seen"] == "2026-10-15T00:00:00Z" and rows["b"]["last_seen"] == "2026-10-16T00:00:00Z"
    assert rows["c"]["class"] == "team" and rows["c"]["classified_by"] == "agent"  # an agent's classification stays
    db.close()


def test_exceptions_at_the_source_boundary_are_isolated():
    from datetime import date

    from docket.sources.collect import Starts, read_sources

    class WS:
        root = None

        class config:
            transports = {}
            sources = {sid: Source.model_validate({
                "kind": "files", "path": path, "request": {"read": {"path": path}},
                "paging": {"entries": "$.entries", "next": None}}) for sid, path in (("good", "x.jsonl"),
                                                                                     ("bad", "y.jsonl"))}

    import tempfile
    from pathlib import Path
    root = Path(tempfile.mkdtemp())
    (root / "x.jsonl").write_text('{"id": "a"}\n')
    WS.root = root

    from docket.transports import files as files_mod
    orig = files_mod.FilesTransport.request

    async def boom(self, req):
        if req.payload.get("path") == "y.jsonl":
            raise RuntimeError("disk on fire")
        return await orig(self, req)
    files_mod.FilesTransport.request = boom
    try:
        res = asyncio.run(read_sources(WS, ["good", "bad"], env={}, instant="2026-10-15T00:00:00+00:00",
                                       run_date=date(2026, 10, 15), starts=Starts(), log_dir=None, secrets=Secrets()))
    finally:
        files_mod.FilesTransport.request = orig
    assert res["good"].outcome.status == "read"
    assert res["bad"].outcome.status == "failed" and res["bad"].units[0].outcome.cause["message"] == \
        "RuntimeError: disk on fire"
    assert Response().ok
