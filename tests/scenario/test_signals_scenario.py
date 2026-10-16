"""Signals scenarios (IP M3 exit, TP §6.1): the personas' rules, ``item why`` and ``config test``."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from docket.items.mapping import item_id
from docket.store.paths import state_root
from tests.agent_loop import run_agent


def tree(root: Path) -> dict[str, bytes]:
    skip = {"docket.db-wal", "docket.db-shm"}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.name not in skip}


def why(cli, ws, ref):
    code, env = cli("item", "why", ref, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["item"]


def test_item_why_explains_kept_dropped_merged_and_suppressed(persona, cli, pinned_now):
    ws = persona("reference-like")
    code, env = cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    offsite = item_id("mail", "", "<msg-003@northwind.example>")
    db = sqlite3.connect(state_root(ws, os.environ) / "docket.db")
    with db:  # a dismissal recorded by an earlier run, live until the 30th
        db.execute("INSERT INTO dispositions (topic_id, decision, run_id, item, until, note) VALUES (?,?,?,?,?,?)",
                   (offsite, "dismiss", "2026-10-14-daily", offsite, "2026-10-30", None))
    db.close()
    res = run_agent(ws, resume_paused=False)
    assert res.failures == [] and res.final["reason"] == "paused"

    kept = why(cli, ws, "Design review: ingest service v2 #12")
    assert kept["status"] == "kept" and kept["keep"] and "chain-on-owned-topic" in kept["rules"]
    assert any(ln.startswith("[signals] rule chain-on-owned-topic (workspace:signals.yaml:") for ln in kept["lines"])
    assert any(ln.startswith("[merge] score ") for ln in kept["lines"])

    dropped = why(cli, ws, "quarterly goals is ready for comments. (follow-up 19)")
    assert dropped["status"] == "dropped" and dropped["reason"] == "score 47 below floor 55"
    assert dropped["rules"] == ["broadcast-rooms"]
    dismissed = why(cli, ws, "Lunch order closes at noon. (follow-up 22)")
    assert dismissed["status"] == "dropped" and dismissed["rules"] == ["social-chatter"]
    assert dismissed["reason"] == "dismissed: Social chatter"

    merged = why(cli, ws, "schema migration for INC-2041")
    assert merged["merged"] and merged["topic"] == "ref:incident:INC-2041"
    assert merged["rules"] == ["incident-coverage"]
    top = why(cli, ws, merged["topic_item"])
    assert top["source"] == "mail" and top["members"] == [merged["id"]] and top["status"] == "kept"
    assert any(ln.startswith("[merge] merged: basis reference") for ln in top["lines"])

    suppressed = why(cli, ws, offsite)
    assert suppressed["status"] == "suppressed" and suppressed["reason"] == "disposition: dismiss until 2026-10-30"
    assert any(ln.startswith("[merge] suppressed") for ln in suppressed["lines"])

    surfaced = why(cli, ws, "Acme Corp asked again about the export latency numbers. (follow-up 15)")
    assert surfaced["status"] == "kept" and surfaced["score"] == 55  # under the floor, raised by surface: always
    assert {"broadcast-rooms", "customer-signal", "protected-entity"} <= set(surfaced["rules"])

    code, env = cli("item", "list", "--filter", "rule=customer-signal source=chat", "--workspace", str(ws))
    assert code == 0 and {r["status"] for r in env["data"]["items"]} == {"kept"} and len(env["data"]["items"]) == 2
    assert all(r["sensitivity"] == "customer" for r in env["data"]["items"])
    [note] = env["data"]["coverage_notes"]
    assert (note["value"], note["home"], note["home_outcome"]) == ("INC-2041", "wiki", "not collected")
    code, env = cli("item", "list", "--filter", "status=suppressed", "--workspace", str(ws))
    assert [r["id"] for r in env["data"]["items"]] == [offsite]

    code, env = cli("item", "why", "no such words anywhere", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "item.unknown"
    code, env = cli("item", "why", "Lunch order", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "item.ambiguous" and len(env["error"]["details"]["matches"]) == 2
    code, env = cli("item", "list", "--filter", "colour=red", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "item.bad_filter" and env["error"]["details"]["suggestion"] is None


def test_assess_issues_tasks_only_for_items_that_need_them(persona, cli, pinned_now):
    ws = persona("reference-like")
    code, env = cli("run", "start", "signals-digest", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done", res.final
    tasks = state_root(ws, os.environ) / "runs" / run_id / "tasks"
    packs = [json.loads(p.read_text()) for p in sorted(tasks.glob("*/pack.json"))]
    packed = [it for pk in packs if pk["kind"] == "assess" for it in pk["items"]]
    assert packed and not any(it["source"] == "escalations" for it in packed)   # rules-only: no task
    assert not any("Lunch order" in ln["text"] for it in packed for ln in it["lines"])                # dismissed: no task
    assert sum(1 for it in packed if any("Acme Corp asked again" in ln["text"] for ln in it["lines"])) == 2  # must-surface: always assessed

    p1 = why(cli, ws, "ESC-310")
    assert p1["status"] == "kept" and "escalation-p1" in p1["rules"]
    assert any(ln == "[assess] rules_only: rules escalation-p1" for ln in p1["lines"])
    assert all("agent" not in e.get("components", {}) for e in p1["trace"] if e.get("step") == "score")
    closed = why(cli, ws, "ESC-313")
    assert closed["status"] == "dropped" and closed["reason"] == "dismissed: Escalation closed"

    esc = why(cli, ws, "ESC-312")  # the strongest of three items sharing the incident reference
    assert not esc["merged"] and esc["topic"] == "ref:incident:INC-2041" and len(esc["members"]) == 2
    code, env = cli("item", "list", "--filter", "topic=ref:incident:INC-2041", "--workspace", str(ws))
    [inc] = env["data"]["items"]
    assert inc["id"] == esc["id"] and inc["source"] == "escalations"
    assert any(ln.startswith("[signals] rule escalation-deadline") and ln.endswith("extracted deadline 2026-10-20")
               for ln in esc["lines"])


def test_support_lead_rules_are_queue_rules(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws)
    assert res.failures == [] and res.final["kind"] == "done"
    closure = why(cli, ws, "Request to close account (case 15)")    # a P1 ticket: surface beats dismiss
    assert closure["status"] == "kept"
    assert any(ln.startswith("[signals] surface: always (p1-ticket) overrides dismiss by account-closure")
               for ln in closure["lines"])
    assert any(ln.startswith("[decide] question: question q_") for ln in closure["lines"])  # later stages too
    plain = why(cli, ws, "Request to close account (case 25)")
    assert plain["status"] == "dropped" and plain["rules"] == ["p2-ticket", "account-closure"]
    page = why(cli, ws, "Export queue backlog growing")
    assert page["reason"] == "dismissed: Low-severity page"
    code, env = cli("item", "list", "--filter", "tag=strategic", "--workspace", str(ws))
    assert env["data"]["items"] and all(r["sensitivity"] == "customer" for r in env["data"]["items"])


def test_config_test_shows_a_score_change_after_a_weight_edit_without_writing(persona, cli, pinned_now):
    ws = persona("support-lead")
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "decide")
    assert res.failures == []
    root = state_root(ws, os.environ)
    before = tree(root)
    code, env = cli("config", "test", "--workspace", str(ws))
    assert code == 0, env
    assert env["data"]["compared"] and env["data"]["diff"]["changed"] is False  # same configuration, same result

    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace(
        "scoring: { floor: 40 }", "scoring: { floor: 40, weights: { importance: 3, urgency: 1, agent: 1 } }"))
    code, env = cli("config", "test", "--workspace", str(ws))
    assert code == 0, env
    d = env["data"]["diff"]
    assert d["changed"] and env["data"]["written"] is False
    change = next(x for x in d["score_changes"] if x["title"] == "Charged twice for one order")
    assert (change["before"], change["after"]) == (93, 96)    # (100 + 85) / 2 -> (3 * 100 + 85) / 4
    assert tree(root) == before                                 # the state root is unchanged

    cfg.write_text(cfg.read_text().replace("then: { route: worklog, area: Billing, importance: +10 }",
                                           "then: { route: escalate, area: Billing, importance: +10 }"))
    code, env = cli("config", "test", "--workspace", str(ws))
    assert env["data"]["diff"]["route_changes"] and \
        {x["after"] for x in env["data"]["diff"]["route_changes"]} == {"escalate"}
    assert tree(root) == before


def test_item_commands_before_merge_and_config_test_without_records(persona, cli, pinned_now):
    ws = persona("support-lead")
    code, env = cli("config", "test", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "run.no_records"
    cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("item", "why", "anything", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "item.not_merged"


def test_reference_rule_file_expresses_the_predecessor_signal_categories():
    """PRD Appendix B, category by category, on synthetic items against the persona's own rule file."""
    from docket.config import load_workspace
    from docket.signals.engine import env_for, evaluate, needs_agent
    from tests.conftest import PERSONAS
    ws = load_workspace(PERSONAS / "reference-like")
    units = {("chat", "spaces/dm"): {"name": "Direct message", "kind": "direct", "class": "direct", "tier": "1"},
             ("chat", "spaces/bots"): {"name": "Deploy Bots", "kind": "team", "class": "bots", "tier": "3"},
             ("chat", "spaces/alerts"): {"name": "Ingest Alerts", "kind": "team", "class": "alerts", "tier": "3"}}
    env = env_for(ws, units=units, now="2026-10-15T23:00:00+00:00", run_date="2026-10-15")

    def it(i, **kw):
        base = {"id": i, "source": "chat", "unit": "", "identity": i, "title": "", "text": "", "from": "",
                "handle": "", "at": "2026-10-15T12:00:00+00:00", "url": "", "fields": {}, "thread": "",
                "to": [], "cc": [], "mentions": []}
        base.update(kw)
        return base

    rows = [
        it("chain-owned", source="mail", handle="lee@northwind.example", title="ingest service rollout"),
        it("dm-chain", unit="spaces/dm", handle="users/3003", text="quick question"),
        it("dm-report", unit="spaces/dm", handle="users/2002", text="quick question"),
        it("dm-unknown", unit="spaces/dm", handle="users/9999", text="quick question"),
        it("bots", unit="spaces/bots", handle="users/5005", text="deploy 42 finished"),
        it("alerts", unit="spaces/alerts", text="threshold crossed"),
        it("auto-mail", source="mail", handle="noreply@tools.example", title="Your weekly digest"),
        it("invite", source="mail", handle="lee@northwind.example", title="Invitation: Platform Sync"),
        it("compliance", source="mail", handle="noreply@hr.example", title="Mandatory training due Friday"),
        it("customer", text="Acme Corp asked about an outage"),
        it("ask", text="Dana, can you sign off?", handle="users/2002"),
        it("mine-1", thread="t/1", handle="users/1001", at="2026-10-15T08:00:00+00:00", text="kicking this off"),
        it("mine-2", thread="t/1", handle="users/4004", at="2026-10-15T09:00:00+00:00", text="reply"),
        it("inc", text="status of INC-777?"),
        it("esc-p1", source="escalations", fields={"priority": "P1", "status": "open"}, title="Exports failing"),
        it("esc-p3", source="escalations", fields={"priority": "P3", "status": "open"}, title="Minor lag"),
    ]
    out = {x["id"]: x for x in evaluate(rows, env)}
    sig = {k: v["signals"] for k, v in out.items()}
    assert sig["chain-owned"]["surface"] and "chain-on-owned-topic" in sig["chain-owned"]["rules"]
    assert (sig["dm-chain"]["importance"], sig["dm-report"]["importance"], sig["dm-unknown"]["importance"]) == \
        (30, 20, -10)
    assert sig["bots"]["dismissed"] and sig["alerts"]["dismissed"]
    assert sig["auto-mail"]["dismissed"] and sig["invite"]["dismissed"]
    assert sig["compliance"]["surface"] and not sig["compliance"]["dismissed"]  # surface beats automated-mail
    assert sig["customer"]["surface"] and sig["customer"]["sensitivity"] == "customer"
    assert sig["ask"]["surface"] and "direct-ask" in sig["ask"]["rules"]
    assert sig["mine-2"]["surface"] and "user-started-thread" in sig["mine-2"]["rules"]
    assert sig["inc"]["expect_in"] == [{"source": "wiki", "reference": "incident", "rule": "incident-coverage"}]
    assert not needs_agent(out["esc-p1"]) and sig["esc-p1"]["surface"] and sig["esc-p1"]["route"] == "backlog"
    assert not needs_agent(out["esc-p3"]) and sig["esc-p3"]["importance"] == -10
    assert needs_agent(out["dm-report"])
