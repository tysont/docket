"""Collection scenarios (IP M2 exit, TP §6.2): both personas collect from recorded transports
with one source failing, one unavailable and one partial while the others complete;
required sources wait; refresh generations; cursor proposals at close; source inspection."""
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
    code, env = cli("collect", "--workspace", str(ws))
    assert code == 0, env
    src = env["data"]["result"]["sources"]
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
    got = {(u["area"], u["detail"]) for u in env["unexpected"]}
    assert exp["shapes"] <= got
    assert all(u["extend"] for u in env["unexpected"])
    # the run continues: nothing above is required
    assert env["data"]["step"]["kind"] == "run" and env["data"]["step"]["stage"] == "assess" or \
        env["data"]["step"]["kind"] == "dispatch"
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
    code, env = cli("collect", "--workspace", str(ws))
    assert code == 0
    step = env["data"]["step"]
    assert step["kind"] == "wait" and step["reason"] == "sources_unavailable"
    assert "wiki" in step["instructions"] and "stub-login wiki" in step["instructions"]
    assert step["command"]["argv"][1:5] == ["collect", "--refresh", "--sources", "wiki"]
    code, env = cli("next", "--workspace", str(ws))
    assert env["data"]["step"]["reason"] == "sources_unavailable"
    # the operator signs in; the wait's own command reads the source in a new generation
    session.write_text("ok")
    code, env = cli(*step["command"]["argv"][1:])
    assert code == 0, env
    assert env["data"]["generation"] == 1 and env["data"]["sources"] == ["wiki"]
    assert env["data"]["result"]["sources"]["wiki"]["outcome"] == "read"
    assert env["data"]["step"]["kind"] != "wait"
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"


def test_refresh_reads_from_coverage_end_without_moving_the_durable_cursor(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("collect", "--workspace", str(ws))
    assert env["data"]["result"]["sources"]["tickets"]["records"] == 30
    feed = ws / "data" / "tickets.jsonl"
    first = json.loads(feed.read_text().splitlines()[0])
    with feed.open("a") as fh:
        fh.write(json.dumps({**first, "key": "SUP-2001", "summary": "Arrived after the evening collect"}) + "\n")
        fh.write(json.dumps({**first, "summary": "Refund not received (customer replied)"}) + "\n")
    code, env = cli("collect", "--refresh", "--sources", "tickets", "--workspace", str(ws))
    assert code == 0, env
    data = env["data"]
    assert data["generation"] == 1 and data["sources"] == ["tickets"]
    assert data["result"]["sources"]["tickets"]["generations"] == [0, 1]
    assert data["result"]["sources"]["tickets"]["records"] == 31  # union, de-duplicated by record identity
    assert data["result"]["item_count"] == 31 + data["result"]["sources"]["pager"]["records"]
    run_dir = state_root(ws, os.environ) / "runs" / env["run"]
    gen1 = (run_dir / "gen-1" / "records" / "tickets.jsonl").read_text().splitlines()
    assert len(gen1) == 2  # only the lines after generation 0's coverage end were read
    art = json.loads((run_dir / "artifacts" / "collect" / (data["digest"] + ".json")).read_text())
    titles = {i["identity"]: i["title"] for i in art["data"]["items"]}
    assert titles[first["key"]] == "Refund not received (customer replied)"  # the later generation wins
    db = db_of(ws)
    assert db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0  # durable cursors move only at close
    prop = db.execute("SELECT * FROM cursor_proposals WHERE source='tickets'").fetchone()
    assert prop["position"] == "32" and prop["generation"] == 1
    db.close()
    # an unknown source refuses; a refresh with nothing new keeps the union
    code, env = cli("collect", "--refresh", "--sources", "tikets", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_source"
    assert env["error"]["details"]["suggestion"] == "tickets"
    code, env = cli("collect", "--refresh", "--workspace", str(ws))  # the workflow's volatile sources
    assert env["data"]["generation"] == 2 and sorted(env["data"]["sources"]) == ["pager", "tickets"]
    assert env["data"]["result"]["sources"]["tickets"]["records"] == 31
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    db = db_of(ws)
    assert db.execute("SELECT position FROM cursors WHERE source='tickets'").fetchone()[0] == "32"
    db.close()


def test_recorded_refresh_replays_follow_up_requests(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("collect", "--workspace", str(ws))
    before = env["data"]["result"]["item_count"]
    code, env = cli("collect", "--refresh", "--workspace", str(ws))
    assert code == 0 and env["data"]["sources"] == ["chat", "mail"]
    src = env["data"]["result"]["sources"]
    assert src["chat"]["outcome"] in ("read", "empty") and src["mail"]["outcome"] == "empty"
    assert "cause" not in src["chat"] and env["data"]["result"]["item_count"] == before


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


def test_context_records_never_enter_the_item_pipeline(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    text = cfg.read_text().replace("    sources: [tickets, pager]\n", "    sources: [tickets, pager, closed]\n", 1)
    text = text.replace("\nsources:\n", "\nsources:\n  closed:\n    kind: files\n    path: data/history.jsonl\n"
                        "    target: context\n    mapping: { id: \"$.id\", title: \"$.title\" }\n", 1)
    cfg.write_text(text)
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    code, env = cli("collect", "--workspace", str(ws))
    run_dir = state_root(ws, os.environ) / "runs" / env["run"]
    art = json.loads((run_dir / "artifacts" / "collect" / (env["data"]["digest"] + ".json")).read_text())["data"]
    assert len(art["context"]) == 10 and all(c["source"] == "closed" for c in art["context"])
    assert not any(i["source"] == "closed" for i in art["items"])
    assert art["sources"]["closed"]["records"] == 10 and art["sources"]["closed"]["target"] == "context"
