"""Reading scenarios (IP M2 exit, TP §6.2): both personas read from recorded transports, one
read node per source, with one source failing, one unavailable and one partial while the
others complete; a required source waits; cursor proposals at read, durable cursors at the
seal; source inspection."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from docket.store.paths import state_root
from tests.agent_loop import run_agent

ISOLATION = {
    "reference-like": {"read": ["chat", "mail"], "skipped": "transcripts", "failed": "status",
                       "unavailable": "wiki", "partial": "archive",
                       "raw": "HTTP 503: upstream shard 7 offline; retry after maintenance", "status": 503,
                       "shapes": {("source.unit", "chat unit without name"),
                                  ("source.contract", "chat.space.spaceType=THREADED_ROOM"),
                                  ("source.contract", "chat.message.createTime missing")}},
    "support-lead": {"read": ["tickets", "pager"], "skipped": None, "failed": "metrics", "unavailable": "kb",
                     "partial": "history", "raw": 'HTTP 500: {"error":"stats backend timeout after 30s"}',
                     "status": 500, "shapes": {("source.contract", "pager.entry.severity=sev0")}},
}


def read_all(cli, ws, sources):
    """Do each source's read node; return each source's outcome and every unexpected shape."""
    out, unexpected = {}, []
    for sid in sources:
        code, env = cli("do", f"read-{sid}", "--workspace", str(ws))
        assert code == 0, env
        out.update(env["data"]["result"]["sources"])
        unexpected += env["unexpected"]
    return out, unexpected, env


def artifact(ws, run_id, node):
    d = state_root(ws, os.environ) / "runs" / run_id / "artifacts" / node
    return json.loads((d / ((d / "latest").read_text().strip() + ".json")).read_text())["data"]


def db_of(ws):
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    db.row_factory = sqlite3.Row
    return db


@pytest.mark.parametrize("name", sorted(ISOLATION))
def test_isolation_one_failing_one_unavailable_one_partial(persona, cli, pinned_now, name):
    exp = ISOLATION[name]
    ws = persona(name)
    code, env = cli("run", "start", "isolation", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    reads = sorted(n["node"][5:] for n in env["data"]["nodes"] if n["verb"] == "read")
    src, unexpected, env = read_all(cli, ws, reads)
    for sid in exp["read"]:
        assert src[sid]["outcome"] == "read" and src[sid]["records"] > 0, (sid, src[sid])
    if exp["skipped"]:
        assert src[exp["skipped"]]["outcome"] == "skipped" and src[exp["skipped"]]["reason"].startswith("cadence")
    failed = src[exp["failed"]]
    assert failed["outcome"] == "failed"
    assert failed["cause"]["message"] == exp["raw"] and failed["cause"]["status"] == exp["status"]  # verbatim
    un = src[exp["unavailable"]]
    assert un["outcome"] == "unavailable" and "session expired (code 401)" in un["reason"]
    assert "stub-login" in un["instructions"]
    part = src[exp["partial"]]
    assert part["outcome"] == "partial" and part["cause"]["code"] == "source.budget_exhausted"
    assert part["records"] > 0 and part["position"]
    got = {(u["area"], u["detail"]) for u in unexpected}
    assert exp["shapes"] <= got
    assert all(u["extend"] for u in unexpected)
    # the run continues: nothing above is required
    assert ("dispatch", "assess") in {(s["kind"], s["node"]) for s in env["data"]["steps"]}
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    code, env = cli("unexpected", "--workspace", str(ws))
    shapes = {(s["area"], s["detail"]): s for s in env["data"]["shapes"]}
    assert exp["shapes"] <= set(shapes) and all(shapes[k]["extend"] for k in exp["shapes"])
    # cursors: complete sources advance, the partial one advances to its gap, the failed one holds
    db = db_of(ws)
    cur = {(r["source"], r["unit"]): r["position"] for r in db.execute("SELECT * FROM cursors")}
    assert not any(s == exp["failed"] for s, _ in cur) and not any(s == exp["unavailable"] for s, _ in cur)
    assert cur[(exp["partial"], "*")] == part["position"]
    assert any(s == exp["read"][0] for s, _ in cur)
    db.close()


def test_isolation_journal_and_units(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "isolation", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws)
    assert res.final["kind"] == "done"
    journal = open(res.final["summary"]["journal"]).read()
    for line in ("status: failed", "wiki: unavailable", "archive: partial", "transcripts: skipped"):
        assert line in journal
    code, env = cli("units", "list", "--source", "chat", "--workspace", str(ws))
    units = {u["unit_id"]: u for u in env["data"]["units"]}
    assert units["spaces/dm-kim"]["kind"] == "direct" and units["spaces/dm-kim"]["tier"] == "1"
    war = units["spaces/ingest-war-room"]  # the first rule to set an attribute wins it
    assert (war["class"], war["tier"], war["kind"], war["rule"]) == ("incident", "1", "broadcast", "war-room,large-room")
    assert units["spaces/release-train"]["class"] == "broadcast" and units["spaces/release-train"]["members"] == 30
    assert units["spaces/hallway"]["name"] == "spaces/hallway"  # nameless: the id is its name
    assert units["spaces/hallway"]["classified_by"] == "rule"
    # The registry keeps what the system called the unit, whatever kind a rule gave it.
    assert units["spaces/dm-kim"]["type"] == "DM" and units["spaces/release-train"]["type"] == "ROOM"


def test_units_preview_applies_the_current_rules_to_the_registry_and_writes_nothing(persona, cli, pinned_now):
    """Change a rule, see what it would do to every unit already discovered, read nothing."""
    ws = persona("reference-like")
    cli("run", "start", "isolation", "--now", pinned_now, "--workspace", str(ws))
    run_agent(ws)
    code, env = cli("units", "preview", "--workspace", str(ws))
    same = env["data"]
    assert code == 0 and same["changed"] == [] and same["units"] == sum(same["classes"].values())
    before = cli("units", "list", "--workspace", str(ws))[1]["data"]["units"]

    cfg = ws / "signals.yaml"
    text = cfg.read_text()
    assert "{ id: direct, match: { source: chat, type: [DM] }" in text
    cfg.write_text(text.replace("- { id: war-room,", "- { id: trains, match: { source: chat, name_regex: "
                                '"(?i)release train" }, set: { class: releases, tier: 2 } }\n      - { id: war-room,'))
    code, env = cli("units", "preview", "--workspace", str(ws))
    data = env["data"]
    assert code == 0 and data["classes"]["releases"] == 1
    (ch,) = data["changed"]
    assert ch["unit"] == "spaces/release-train" and ch["name"]
    assert (ch["was"]["class"], ch["now"]["class"], ch["now"]["tier"]) == ("broadcast", "releases", "2")
    assert ch["now"]["kind"] == "broadcast" and ch["now"]["rule"] == "trains,large-room"  # kind from the later rule
    # the DM is still matched by its type, though a rule renamed its kind
    assert not any(c["unit"] == "spaces/dm-kim" for c in data["changed"])
    assert cli("units", "list", "--workspace", str(ws))[1]["data"]["units"] == before  # nothing written

    code, env = cli("units", "preview", "--class", "releases", "--workspace", str(ws))
    assert [u["unit"] for u in env["data"]["listed"]] == ["spaces/release-train"]
    code, env = cli("units", "preview", "--class", "none", "--workspace", str(ws))
    assert all(u["class"] is None for u in env["data"]["listed"])


WIKI_RECORDING = json.dumps({
    "request": {"op": "read", "args": {"page_size": 100}},
    "response": {"body_json": {"v": 1, "id": 1, "ok": True, "result": {
        "items": [{"id": "W-1", "title": "Runbook page updated", "at": "2026-10-15T09:00:00Z"}],
        "next": "1", "end": True}}}}) + "\n"


def test_required_unavailable_source_waits_then_its_command_reads_it(persona, cli, pinned_now, tmp_path):
    ws = persona("reference-like")
    session = tmp_path / "wiki-session"
    srcs = ws / "sources.yaml"
    srcs.write_text(srcs.read_text().replace("    transport: wiki-feed\n", "    transport: wiki-feed\n    required: true\n")
                    .replace("check: [docket-stub-auth, --fail]", f"check: [docket-stub-auth, --if-file, '{session}']"))
    (ws / "recordings" / "wiki-feed.jsonl").write_text(WIKI_RECORDING)
    cli("run", "start", "isolation", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("do", "read-wiki", "--workspace", str(ws))
    assert code == 0
    (step,) = [s for s in env["data"]["steps"] if s["node"] == "read-wiki"]
    assert step["kind"] == "wait" and step["reason"] == "source_unavailable"
    assert step["command"]["argv"][1:4] == ["do", "read-wiki", "--again"]
    code, env = cli("next", "--workspace", str(ws))
    (wait,) = [s for s in env["data"]["steps"] if s["node"] == "read-wiki"]
    assert wait["reason"] == "source_unavailable"
    assert "wiki" in wait["instructions"] and "stub-login wiki" in wait["instructions"]
    # a node that waits holds back only what needs it: the other reads are still ready
    assert {s["node"] for s in env["data"]["steps"]} >= {"read-chat", "read-mail"}
    # the operator signs in; the wait's own command reads the source again
    session.write_text("ok")
    code, env = cli(*step["command"]["argv"][1:])
    assert code == 0, env
    assert env["data"]["result"]["sources"]["wiki"]["outcome"] == "read"
    assert not any(s["node"] == "read-wiki" for s in env["data"]["steps"])
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"


def test_a_read_proposes_cursors_and_only_the_seal_moves_them(persona, cli, pinned_now):
    ws = persona("support-lead")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    code, env = cli("do", "read-tickets", "--workspace", str(ws))
    assert env["data"]["result"]["sources"]["tickets"]["records"] == 30
    assert env["data"]["result"]["item_count"] == 30
    read = artifact(ws, run_id, "read-tickets")
    assert set(read["sources"]) == {"tickets"} and len(read["items"]) == 30
    assert all(it["source"] == "tickets" for it in read["items"])
    db = db_of(ws)
    assert db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0  # durable cursors move only at the seal
    prop = db.execute("SELECT * FROM cursor_proposals WHERE source='tickets'").fetchone()
    assert prop["position"] == "30" and prop["run_id"] == run_id
    db.close()
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    db = db_of(ws)
    assert db.execute("SELECT position FROM cursors WHERE source='tickets'").fetchone()[0] == "30"
    db.close()


def test_recorded_follow_up_requests_replay_in_a_later_run(persona, cli, pinned_now):
    """The recordings hold the requests a run makes from the cursors of the run before it."""
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    assert run_agent(ws).final["kind"] == "done"
    code, env = cli("run", "start", "daily", "--now", pinned_now, "--new", "--workspace", str(ws))
    assert code == 0 and env["data"]["created"] is True
    src, _, env = read_all(cli, ws, ["chat", "mail"])
    assert src["chat"]["outcome"] in ("read", "empty") and src["mail"]["outcome"] == "empty"
    assert "cause" not in src["chat"] and "cause" not in src["mail"]


def test_source_test_samples_and_writes_nothing(persona, cli, pinned_now):
    ws = persona("reference-like")
    code, env = cli("source", "test", "chat", "--limit", "3", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    assert len(env["data"]["items"]) == 3 and env["data"]["written"] is False
    assert {d["id"] for d in env["data"]["discovered"]} >= {"spaces/dm-kim", "spaces/hallway"}
    assert not (state_root(ws, os.environ) / "docket.db").exists()
    code, env = cli("source", "test", "chat", "--unit", "spaces/dm-kim", "--now", pinned_now, "--workspace", str(ws))
    assert {it["unit"] for it in env["data"]["items"]} == {"spaces/dm-kim"}
    code, env = cli("source", "test", "status", "--now", pinned_now, "--workspace", str(ws))
    assert code == 5 and env["error"]["code"] == "transport.status"
    assert env["error"]["message"] == "HTTP 503: upstream shard 7 offline; retry after maintenance"
    code, env = cli("source", "test", "wiki", "--workspace", str(ws))
    assert code == 5 and env["error"]["code"] == "source.auth_failed" and "stub-login" in env["error"]["hint"]
    code, env = cli("source", "test", "chatt", "--workspace", str(ws))
    assert code == 2 and env["error"]["details"]["suggestion"] == "chat"
    assert not (state_root(ws, os.environ) / "runs").exists()


def test_context_records_never_become_items(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    text = cfg.read_text().replace("    assess: { judge: assess, from: [read-tickets, read-pager] }\n",
                                   "    read-closed: { read: closed }\n"
                                   "    assess: { judge: assess, from: [read-tickets, read-pager, read-closed] }\n", 1)
    text = text.replace("\nsources:\n", "\nsources:\n  closed:\n    kind: files\n    path: data/history.jsonl\n"
                        "    target: context\n    mapping: { id: \"$.id\", title: \"$.title\" }\n", 1)
    cfg.write_text(text)
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    run_id = env["data"]["run"]
    code, env = cli("do", "read-closed", "--workspace", str(ws))
    assert code == 0, env
    art = artifact(ws, run_id, "read-closed")
    assert len(art["context"]) == 10 and all(c["source"] == "closed" for c in art["context"])
    assert art["items"] == []
    assert art["sources"]["closed"]["records"] == 10 and art["sources"]["closed"]["target"] == "context"
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    packed = [it for s in res.steps if s["kind"] == "dispatch" for t in s["tasks"]
              for it in json.loads(open(t["pack_path"]).read())["items"]]
    assert packed and not any(it["source"] == "closed" for it in packed)
    assert not any(it["source"] == "closed" for it in artifact(ws, run_id, "merge")["items"])


def test_a_read_reports_membership_and_the_operator_can_classify_a_unit(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("do", "read-chat", "--workspace", str(ws))
    chat = env["data"]["result"]["sources"]["chat"]
    assert chat["membership"]["baseline"] is True and chat["membership"]["new"] == 0
    assert chat["membership"]["current"] == len(cli("units", "list", "--source", "chat", "--workspace", str(ws))[1]
                                                 ["data"]["units"])
    # A unit with records and no class is named, so a new kind of room is noticed in the run that meets it.
    assert all(set(u) == {"unit", "name", "records"} for u in chat["membership"].get("unclassified_active", []))

    code, env = cli("units", "set", "chat", "spaces/hallway", "--class", "social", "--tier", "4",
                    "--workspace", str(ws))
    assert code == 0 and env["data"]["unit"]["classified_by"] == "operator" and env["data"]["unit"]["class"] == "social"
    code, env = cli("units", "list", "--source", "chat", "--workspace", str(ws))
    assert {u["unit_id"]: u["class"] for u in env["data"]["units"]}["spaces/hallway"] == "social"
    code, env = cli("units", "preview", "--class", "social", "--workspace", str(ws))
    assert [u["unit"] for u in env["data"]["listed"]] == ["spaces/hallway"] and env["data"]["changed"] == []

    code, env = cli("units", "set", "chat", "spaces/nope", "--class", "x", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_unit"
    code, env = cli("units", "set", "chat", "spaces/hallway", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_argument"

    code, env = cli("units", "unset", "chat", "spaces/hallway", "--workspace", str(ws))
    assert code == 0 and env["data"]["unit"]["classified_by"] == "rule" and env["data"]["unit"]["class"] != "social"

    code, env = cli("units", "list", "--unclassified", "--workspace", str(ws))
    assert all(u["class"] is None for u in env["data"]["units"])
