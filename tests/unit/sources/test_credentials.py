"""Credential references (TD §6.3, §16): a value read from an environment variable, a
whole file, or one string inside a JSON file selected by ``json_path``."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from docket.config import check
from docket.transports.base import Secrets, resolve_credential

pytestmark = pytest.mark.smoke

FAKE = "fake-token-0001"
# A key holding dots, a slash and a bar: a path is a list of keys, never a dotted string.
STORE_KEY = "gateway|https://gw.example.org/mcp"


def ref(**kw):
    return SimpleNamespace(env=kw.get("env"), file=kw.get("file"), json_path=kw.get("json_path"))


def store(tmp_path: Path, body) -> str:
    p = tmp_path / "auth.json"
    p.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(p)


def test_json_path_selects_one_string_inside_a_json_file(tmp_path):
    path = store(tmp_path, {STORE_KEY: {"tokens": {"access_token": FAKE, "refresh_token": "fake-refresh-0002"}}})
    secrets = Secrets()
    val, problem = resolve_credential(ref(file=path, json_path=[STORE_KEY, "tokens", "access_token"]), {}, tmp_path, secrets)
    assert (val, problem) == (FAKE, None)
    assert secrets.mask(f"Bearer {FAKE}") == "Bearer [redacted]"
    assert "fake-refresh-0002" in secrets.mask("fake-refresh-0002")  # only the selected value is registered


def test_json_path_indexes_a_list_by_position(tmp_path):
    path = store(tmp_path, {"accounts": [{"token": "fake-token-zero"}, {"token": FAKE}]})
    val, problem = resolve_credential(ref(file=path, json_path=["accounts", 1, "token"]), {}, tmp_path, Secrets())
    assert (val, problem) == (FAKE, None)


@pytest.mark.parametrize("body,json_path,expect", [
    ({"a": {"b": FAKE}}, ["a", "missing"], "has no key 'missing' at a"),
    ({"a": {"b": FAKE}}, ["a", "b", "c"], "has no key 'c' at a.b"),
    ({"a": [FAKE]}, ["a", 3], "has no index 3 at a"),
    ({"a": {"b": 12}}, ["a", "b"], "is not a string at a.b"),
    ({"a": {"b": ""}}, ["a", "b"], "is empty at a.b"),
    ("{not json " + FAKE, ["a"], "is not valid JSON"),
])
def test_json_path_problems_name_the_place_and_never_a_value(tmp_path, body, json_path, expect):
    path = store(tmp_path, body)
    val, problem = resolve_credential(ref(file=path, json_path=json_path), {}, tmp_path, Secrets())
    assert val is None and expect in problem and path in problem
    assert FAKE not in problem


def test_a_file_without_json_path_is_still_read_whole(tmp_path):
    p = tmp_path / "token"
    p.write_text(FAKE + "\n", encoding="utf-8")
    assert resolve_credential(ref(file=str(p)), {}, tmp_path, Secrets()) == (FAKE, None)


BASE = """\
version: 1
graphs:
  daily: { touch: { run: ["true"] } }
profile:
  timezone: UTC
  me: { name: Dana Reyes }
"""


def issues_of(tmp_path, extra):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docket.yaml").write_text(BASE + textwrap.dedent(extra), encoding="utf-8")
    ws, issues, _ = check(root)
    return ws, issues


def test_configuration_accepts_json_path_with_a_file(tmp_path):
    ws, issues = issues_of(tmp_path, """\
        transports:
          gw:
            kind: mcp_http
            url: "http://127.0.0.1:9/mcp"
            bearer: { file: ~/.config/agent/auth.json, json_path: ["gateway|https://gw.example.org/mcp", tokens, access_token] }
        """)
    assert issues == [] and ws is not None
    assert ws.config.transports["gw"].bearer.json_path == [STORE_KEY, "tokens", "access_token"]


def test_configuration_refuses_json_path_without_a_file(tmp_path):
    _, issues = issues_of(tmp_path, """\
        transports:
          gw:
            kind: mcp_http
            url: "http://127.0.0.1:9/mcp"
            bearer: { env: GATEWAY_TOKEN, json_path: [tokens, access_token] }
        """)
    assert [(i.code, i.path) for i in issues] == [("config.bad_credential", "transports.gw.bearer")]
    assert "json_path" in issues[0].message and "file" in issues[0].message
