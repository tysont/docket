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
     "membershipCount": {"joinedDirectHumanUserCount": 41}, "lastActiveTime": "2026-10-15T09:00:00Z"},
    {"name": "spaces/dm-1", "spaceType": "DIRECT_MESSAGE", "memberCount": 2},
], "nextPageToken": "p2"}
MESSAGES = {"messages": [
    {"name": "spaces/room-1/messages/t1.m1", "createTime": "2026-10-15T07:00:00Z", "text": "x" * 3000,
     "sender": {"name": "users/2002", "displayName": "Kim Osei", "type": "HUMAN"},
     "thread": {"name": "spaces/room-1/threads/t1"}, "cardsV2": [{"cardId": "c"}],
     "annotations": [
         {"type": "USER_MENTION", "startIndex": 0, "length": 4,
          "userMention": {"user": {"name": "users/1001", "displayName": "Dana Reyes", "type": "HUMAN"}, "type": "MENTION"}},
         {"type": "RICH_LINK", "richLinkMetadata": {"uri": "https://docs.example.org/x"}},
         {"type": "USER_MENTION", "userMention": {"user": {"name": "users/3003"}}}]},
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
    assert got["calls"] == [["rooms_list", {}]]  # an absent value is left out: a strict tool schema refuses null
    assert got["out"] == {"nextPageToken": "p2", "spaces": [
        {"name": "spaces/room-1", "displayName": "Platform", "spaceType": "SPACE", "memberCount": 41,
         "lastActiveTime": "2026-10-15T09:00:00Z"},
        {"name": "spaces/dm-1", "displayName": None, "spaceType": "DIRECT_MESSAGE", "memberCount": 2,
         "lastActiveTime": None}]}


@pytest.mark.parametrize("wrapping", WRAPPINGS)
def test_chat_read_calls_the_tool_with_the_workspace_argument_names(wrapping):
    src = _load()
    args = {"tool": "posts_list", "space": "spaces/room-1", "after": "2026-10-14T00:00:00Z", "page_size": 25,
            "token": "t9", "space_arg": "spaceName", "size_arg": "maxResults", "after_arg": "since", "token_arg": "cursor"}
    got = run(src["request"]["read"]["script"], args, wrapping, {"posts_list": MESSAGES})
    assert got["calls"] == [["posts_list", {"spaceName": "spaces/room-1", "since": "2026-10-14T00:00:00Z",
                                            "maxResults": 25, "cursor": "t9"}]]
    (m,) = got["out"]["messages"]
    assert m["name"] == "spaces/room-1/messages/t1.m1" and len(m["text"]) == 2000 and "cardsV2" not in m
    assert m["sender"] == {"name": "users/2002", "displayName": "Kim Osei", "type": "HUMAN"}
    assert m["mentions"] == ["users/1001", "users/3003"]  # people mentioned, by handle; other annotations ignored
    assert m["url"] is None                                # no link template configured
    assert got["out"]["nextPageToken"] is None and got["out"]["truncated"] is False


def test_chat_requests_leave_out_arguments_that_have_no_value_and_pass_those_that_do():
    src = _load()
    got = run(src["request"]["discover"]["script"], {"tool": "rooms_list", "token": "p2", "token_arg": "cursor"},
              "object", {"rooms_list": SPACES})
    assert got["calls"] == [["rooms_list", {"cursor": "p2"}]]
    sized = {"tool": "rooms_list", "token": None, "token_arg": "cursor", "size_arg": "limit", "page_size": 500}
    got = run(src["request"]["discover"]["script"], sized, "object", {"rooms_list": SPACES})
    assert got["calls"] == [["rooms_list", {"limit": 500}]]
    unsized = {**sized, "size_arg": ""}  # no size argument named: the page size is not sent under any name
    got = run(src["request"]["discover"]["script"], unsized, "object", {"rooms_list": SPACES})
    assert got["calls"] == [["rooms_list", {}]]
    first = {"tool": "posts_list", "space": "spaces/room-1", "after": None, "page_size": 25, "token": None,
             "space_arg": "parent", "size_arg": "pageSize", "after_arg": "after", "token_arg": "pageToken"}
    got = run(src["request"]["read"]["script"], first, "object", {"posts_list": MESSAGES})
    assert got["calls"] == [["posts_list", {"parent": "spaces/room-1", "pageSize": 25}]]


READ = {"tool": "posts_list", "space": "spaces/room-1", "after": None, "page_size": 25, "token": None,
        "space_arg": "parent", "size_arg": "pageSize", "after_arg": "after", "token_arg": "pageToken"}


@pytest.mark.parametrize("template,want", [
    ("https://chat.example.org/room/{space}/{thread}", "https://chat.example.org/room/room-1/t1"),
    ("https://chat.example.org/dm/{space}", "https://chat.example.org/dm/room-1"),
    # A link to the message itself. A message id may carry its thread as a prefix
    # ("<thread>.<message>"); {message} is the part that names the message.
    ("https://chat.example.org/room/{space}/{thread}/{message}", "https://chat.example.org/room/room-1/t1/m1"),
    ("", None),
])
def test_chat_read_builds_a_link_back_from_the_workspace_template(template, want):
    src = _load()
    got = run(src["request"]["read"]["script"], {**READ, "link": template}, "object", {"posts_list": MESSAGES})
    assert got["out"]["messages"][0]["url"] == want


def test_the_chat_mapping_carries_link_mentions_and_sender_type():
    src = _load()
    assert src["mapping"]["url"] == "$.url" and src["mapping"]["mentions"] == "$.mentions"
    assert src["mapping"]["fields.sender_type"] == "$.sender.type"
    args = src["request"]["read"]["args"]
    assert args["link"] == "{params.link}" and src["params"]["link"] == ""


def test_the_preset_defaults_name_arguments_and_the_request_plan_passes_them():
    src = _load()
    names = {k: v for k, v in src["params"].items() if k.endswith("_arg") or k.endswith("_tool")}
    assert names == {"spaces_tool": None, "messages_tool": None, "space_arg": "parent",
                     "size_arg": "pageSize", "after_arg": "after", "token_arg": "pageToken",
                     "spaces_size_arg": ""}
    read_args = src["request"]["read"]["args"]
    assert read_args["space_arg"] == "{params.space_arg}" and read_args["token_arg"] == "{params.token_arg}"
    assert src["request"]["discover"]["args"]["token_arg"] == "{params.token_arg}"


def test_an_empty_result_is_an_empty_page():
    src = _load()
    code = HARNESS % render_script(src["request"]["discover"]["script"], {"tool": "rooms_list", "token_arg": "pageToken"})
    p = subprocess.run([NODE, "-e", code, "text", json.dumps({"rooms_list": None})], capture_output=True, text=True, timeout=20)
    # JSON.stringify(null) is "null": a result with no spaces is an empty page, not a crash.
    assert p.returncode == 0 and json.loads(p.stdout)["out"] == {"spaces": [], "nextPageToken": None}
