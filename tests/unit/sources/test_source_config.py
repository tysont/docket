"""Source and transport configuration (TD §3.4, §6.1, §6.3, §16): presets through
``extends`` with origins, references, credential references and secret literals."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from docket.config import check, explain
from docket.config.source_checks import looks_secret

pytestmark = pytest.mark.smoke

BASE = """\
version: 1
workflows:
  daily:
    stages: [collect, assess, merge, decide, plan, approve, apply, close]
profile:
  timezone: UTC
  me: { name: Dana Reyes }
destinations:
  backlog: { adapter: markdown_tasks, path: tasks.md }
"""


def ws_with(tmp_path: Path, extra: str) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docket.yaml").write_text(BASE + textwrap.dedent(extra), encoding="utf-8")
    return root


def issues_of(tmp_path, extra):
    ws, issues, _ = check(ws_with(tmp_path, extra))
    return ws, issues


def test_extends_merges_the_preset_under_the_instance_with_origins(tmp_path):
    ws, issues = issues_of(tmp_path, """\
        transports:
          gw: { kind: codemode, command: [gateway-bin], tool: run_code }
        sources:
          chat:
            extends: mcp/chat-codemode
            transport: gw
            params: { spaces_tool: list_rooms, messages_tool: list_posts }
            paging: { page_size: 25 }
        """)
    assert issues == [] and ws is not None
    src = ws.config.sources["chat"]
    assert src.cursor.model == "units" and src.paging.page_size == 25 and src.paging.min_page_size == 10
    assert src.contract["message"].required["createTime"] == "timestamp"
    assert explain(ws, "sources.chat.paging.page_size")["origin"].startswith("workspace:docket.yaml:")
    assert explain(ws, "sources.chat.paging.min_page_size")["origin"].startswith("preset:mcp/chat-codemode@")


def test_a_files_source_extends_files_jsonl_by_default(tmp_path):
    ws, issues = issues_of(tmp_path, """\
        sources:
          feed: { kind: files, path: data/feed.jsonl }
        """)
    assert issues == [] and ws.config.sources["feed"].extends == "files/jsonl"
    assert ws.config.sources["feed"].cursor.position == "token"


def test_references_params_and_capabilities_are_checked(tmp_path):
    _, issues = issues_of(tmp_path, """\
        transports:
          gateway: { kind: mcp_http, url: "http://127.0.0.1:9/mcp" }
          files-only: { kind: files }
        sources:
          mail: { extends: mcp/mail, transport: gatewya }
          mail2: { extends: mcp/mail, transport: gateway }
          rooms: { extends: mcp/chat-codemode, transport: files-only, params: { spaces_tool: a, messages_tool: b } }
          nope: { extends: mcp/no-such-preset, transport: gateway }
        signals:
          units:
            rules:
              - { match: { source: chatt, name_regex: "(" }, set: { class: team } }
        """)
    by = {(i.code, i.path) for i in issues}
    ref = next(i for i in issues if i.path == "sources.mail.transport")
    assert ref.code == "config.bad_reference" and ref.suggestion == "gateway" and ref.line
    assert ("config.missing_key", "sources.mail2.params.search_tool") in by
    assert ("config.capability_missing", "sources.rooms") in by  # a files transport cannot discover
    assert ("config.unknown_preset", "sources.nope.extends") in by
    assert ("config.bad_reference", "signals.units.rules[0].match.source") in by
    assert ("config.bad_regex", "signals.units.rules[0].match.name_regex") in by


def test_credentials_are_references_and_secret_literals_are_refused(tmp_path):
    _, issues = issues_of(tmp_path, """\
        transports:
          ok:
            kind: http_json
            url: "http://127.0.0.1:9"
            bearer: { env: STATUS_API_TOKEN }
            headers_from: { X-Api-Key: { file: ~/.config/status/key } }
          bad:
            kind: http_json
            url: "http://127.0.0.1:9"
            headers: { Authorization: "Bearer q8Zt3VwX0pLm7Rk2" }
            env: { API_TOKEN: "Zq8x1NcV7tR3pL0wYb5kM2sD" }
        sources:
          s:
            extends: http/json-api
            transport: ok
            params: { path: /v1/items, token: "hJ4kL9mN2pQ7rS1tV5wX8yZ" }
        """)
    secret = sorted(i.path for i in issues if i.code == "config.secret_literal")
    assert secret == ["sources.s.params.token", "transports.bad.env.API_TOKEN", "transports.bad.headers.Authorization"]
    for i in issues:
        assert "q8Zt3" not in i.message and "Zq8x1" not in i.message  # the error never echoes the value


def test_looks_secret():
    assert looks_secret("Bearer abcdefgh12")
    assert looks_secret("Zq8x1NcV7tR3pL0wYb5kM2sD")
    assert not looks_secret("application/json") and not looks_secret("in:inbox newer_than:2d")
    assert not looks_secret("aaaaaaaaaaaaaaaaaaaa")


@pytest.mark.parametrize("name", ["reference-like", "support-lead"])
def test_persona_sources_use_presets_and_recorded_transports(persona, cli, name):
    ws = persona(name)
    code, env = cli("source", "list", "--workspace", str(ws))
    assert code == 0
    rows = {r["id"]: r for r in env["data"]["sources"]}
    assert {r["kind"] for r in rows.values()} >= {"replay"}
    assert any(r["extends"] and r["extends"].startswith("exec/") for r in rows.values())
