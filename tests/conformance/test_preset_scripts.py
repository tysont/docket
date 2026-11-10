"""The scripts a codemode preset ships are real programs. Each one is rendered the way the
transport renders it and run under Node against a stand-in for the gateway's ``codemode``
object, so a preset is checked against the shapes gateways return, not only as text.

A gateway may hand a tool result back as a parsed object, as JSON text, or as MCP content
(a list of ``{type: "text", text: <json>}`` blocks, bare or under ``content``). The
workspace names the tools and the argument names the tools take.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from docket.transports.codemode import render_script

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SPACES = {"spaces": [
    {"name": "spaces/room-1", "displayName": "Platform", "spaceType": "SPACE", "extra": "dropped",
     "membershipCount": {"joinedDirectHumanUserCount": 41}},
    {"name": "spaces/dm-1", "spaceType": "DIRECT_MESSAGE", "memberCount": 2},
], "nextPageToken": "p2"}
MESSAGES = {"messages": [
    {"name": "spaces/room-1/messages/m1", "createTime": "2026-10-15T07:00:00Z", "text": "x" * 3000,
     "sender": {"name": "users/2002", "displayName": "Kim Osei", "type": "HUMAN"},
     "thread": {"name": "spaces/room-1/threads/t1"}, "cardsV2": [{"cardId": "c"}]},
]}

HARNESS = """
const calls = [];
const wrap = { object: v => v, text: v => JSON.stringify(v),
               blocks: v => [{ type: "text", text: JSON.stringify(v) }],
               content: v => ({ content: [{ type: "text", text: JSON.stringify(v) }] }) }[process.argv[1]];
const results = JSON.parse(process.argv[2]);
const codemode = new Proxy({}, { get: (_, name) => async (args) => { calls.push([name, args]); return wrap(results[name]); } });
const script = %s;
script().then(out => console.log(JSON.stringify({ out: JSON.parse(out), calls })));
"""


def _load() -> dict:
    from importlib import resources
    from ruamel.yaml import YAML
    text = resources.files("docket.presets").joinpath("mcp/chat-codemode.yaml").read_text(encoding="utf-8")
    return YAML(typ="safe").load(text)["source"]


def run(script: str, args: dict, wrapping: str, results: dict) -> dict:
    code = HARNESS % render_script(script, args)
    p = subprocess.run([NODE, "-e", code, wrapping, json.dumps(results)], capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr[-600:]
    return json.loads(p.stdout)


WRAPPINGS = ["object", "text", "blocks", "content"]


@pytest.mark.parametrize("wrapping", WRAPPINGS)
def test_chat_discover_projects_spaces_from_every_result_wrapping(wrapping):
    src = _load()
    got = run(src["request"]["discover"]["script"], {"tool": "rooms_list", "token": None, "token_arg": "cursor"},
              wrapping, {"rooms_list": SPACES})
    assert got["calls"] == [["rooms_list", {"cursor": None}]]
    assert got["out"] == {"nextPageToken": "p2", "spaces": [
        {"name": "spaces/room-1", "displayName": "Platform", "spaceType": "SPACE", "memberCount": 41},
        {"name": "spaces/dm-1", "displayName": None, "spaceType": "DIRECT_MESSAGE", "memberCount": 2}]}


@pytest.mark.parametrize("wrapping", WRAPPINGS)
def test_chat_read_calls_the_tool_with_the_workspace_argument_names(wrapping):
    src = _load()
    args = {"tool": "posts_list", "space": "spaces/room-1", "after": "2026-10-14T00:00:00Z", "page_size": 25,
            "token": "t9", "space_arg": "spaceName", "size_arg": "maxResults", "after_arg": "since", "token_arg": "cursor"}
    got = run(src["request"]["read"]["script"], args, wrapping, {"posts_list": MESSAGES})
    assert got["calls"] == [["posts_list", {"spaceName": "spaces/room-1", "since": "2026-10-14T00:00:00Z",
                                            "maxResults": 25, "cursor": "t9"}]]
    (m,) = got["out"]["messages"]
    assert m["name"] == "spaces/room-1/messages/m1" and len(m["text"]) == 2000 and "cardsV2" not in m
    assert m["sender"] == {"name": "users/2002", "displayName": "Kim Osei"}
    assert got["out"]["nextPageToken"] is None and got["out"]["truncated"] is False


def test_the_preset_defaults_name_arguments_and_the_request_plan_passes_them():
    src = _load()
    assert src["params"] == {"spaces_tool": None, "messages_tool": None, "space_arg": "parent",
                             "size_arg": "pageSize", "after_arg": "after", "token_arg": "pageToken"}
    read_args = src["request"]["read"]["args"]
    assert read_args["space_arg"] == "{params.space_arg}" and read_args["token_arg"] == "{params.token_arg}"
    assert src["request"]["discover"]["args"]["token_arg"] == "{params.token_arg}"


def test_an_empty_result_is_an_empty_page():
    src = _load()
    code = HARNESS % render_script(src["request"]["discover"]["script"], {"tool": "rooms_list", "token_arg": "pageToken"})
    p = subprocess.run([NODE, "-e", code, "text", json.dumps({"rooms_list": None})], capture_output=True, text=True, timeout=20)
    # JSON.stringify(null) is "null": a result with no spaces is an empty page, not a crash.
    assert p.returncode == 0 and json.loads(p.stdout)["out"] == {"spaces": [], "nextPageToken": None}
